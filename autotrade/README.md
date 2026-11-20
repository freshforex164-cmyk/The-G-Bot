# autotrade - multi-user auto-trading module

Separate service. It is not in `render.yaml`, and GBot's signal scanner still never trades. GBot only
*controls* this service through the optional `/autotrade` Telegram commands (`autotrade_client.py`,
`autotrade_commands.py` at the repo root), which stay "not set up" until you deploy the service and
set `AUTOTRADE_API_URL` + `AUTOTRADE_JWT_SECRET` on GBot.

## Layout
```text
autotrade/
  schema.sql                 Postgres tables (credentials, settings, trades, bot_state, audit)
  config.py  clock.py  errors.py  logging_utils.py  runtime.py  main.py
  auth/      ssid.py (parse/validate) | handshake.py (Playwright capture) | health.py (session check + worker)
  vault/     crypto.py (AES-256-GCM, key ring) | service.py (TokenVault)
  market/    feed.py                 ONE shared tick listener, fan-out to all workers
  trading/   models.py (settings) | strategy.py (plug-ins) | risk.py | broker.py (BinaryOptionsTools-v2 adapter)
             worker.py (one task + one WebSocket per user) | manager.py (supervisor)
  storage/   repo.py (interface + in-memory) | postgres.py
  api/       app.py (FastAPI) | login_page.html (remote-browser login page, opened from Telegram)
  tests/     72 offline tests (+3 Postgres tests when AUTOTRADE_TEST_DATABASE_URL is set)
  Dockerfile                 image with Chromium (build from repo root)

repo root (GBot side, standard library only, no new GBot dependencies)
  autotrade_client.py        signs short-lived tokens for tg:<telegram user id> and calls the service
  autotrade_commands.py      /autotrade, /at_* Telegram commands
  tests/test_autotrade_commands.py
```

## How it fits together
1. `POST /auth/handshake` starts an isolated Chromium on the server at the Pocket Option login page.
   The frontend shows it with `GET .../screen` (JPEG, poll ~2/s) and sends clicks/typing to `POST .../input`.
   The user completes login, CAPTCHA and 2FA there. We read only the WebSocket `42["auth",{...}]` frame,
   take the Demo and Real SSIDs, **close the browser**, then hand both to the vault.
2. Vault: each SSID is AES-256-GCM encrypted; the user id and account kind are bound as associated data.
   Key ring supports rotation (`TokenVault.rewrap`).
3. `POST /trading/start` health-checks the chosen account's SSID (live connect). Rejected ->
   `409 reauth_required` (frontend re-runs the handshake). Network trouble -> `503`, token kept.
4. One shared feed (`AUTOTRADE_FEED_SSID`, a dedicated Demo account) streams each symbol once. Each user
   worker has its own broker connection for orders and results only.
5. Every order goes strategy -> `RiskManager` -> payout check -> broker. Strategies cannot skip risk.

## Endpoints (Bearer JWT, `sub` = your internal user id)
| Method | Path | Purpose |
|---|---|---|
| POST | /trading/start | Start the user's bot (409 `reauth_required` if the session is missing/expired) |
| POST | /trading/stop | Stop; `{"drain":false}` skips waiting for open trades |
| PATCH | /trading/settings | Change settings. Live needs `{"mode":"live","confirm_live":true}` |
| GET | /trading/settings, /trading/stats | Settings; status, today's P/L, win rate, recent trades, session status |
| GET/POST/DELETE | /auth/status, /auth/handshake[...] | Login capture; `DELETE /auth/sessions` revokes tokens and stops the bot |
| POST | /admin/halt, /admin/resume | Operator kill switch (JWT claim `role=admin`) |
| GET | /healthz | Feed state |
| GET | /login | Login page. Token in the URL fragment `#t=...`; a `scope=login` token works only on `/auth/handshake*` |

## Telegram commands (GBot)
Private chat with the bot only. Each Telegram user is service user `tg:<telegram user id>`.

| Command | What it does |
|---|---|
| /autotrade | Overview: mode, bot status, Demo/Real session status, today's P/L, settings |
| /at_login | Sends a "Log in to Pocket Option" button (Telegram Mini App) with a 15-minute login-only link |
| /at_start, /at_stop | Start; stop (waits for open trades to finish) |
| /at_stats | Today + all-time results and the last 5 trades |
| /at_set name value | `stake`, `expiry`, `max_trades`, `stop_loss`, `take_profit`, `cooldown`, `min_payout`, `gap`; no arguments shows current values |
| /at_live confirm | Switch to Live (without `confirm` it only shows the warning) |
| /at_demo | Back to Demo |
| /at_logout | Stop the bot and delete the stored sessions |
| /at_halt, /at_resume | Admin, in the admin chat only: global kill switch |

Expired session -> the bot answers "send /at_login". Service down -> "can't be reached"; other users
are not affected, and slow calls run in the background so the signal menu never freezes.

## Risk controls (per user)
stake, expiry, max concurrent, daily stop-loss and take-profit (both mandatory, UTC day, realized P/L
plus open-trade exposure), cooldown, minimum payout (read live before each order), max tick age.
New users are **Demo**. Live needs: `AUTOTRADE_ALLOW_LIVE=true` on the service (default false), an active
Real session, an explicit `confirm_live`, and a lower stake cap (`AUTOTRADE_LIVE_MAX_STAKE`, default 10).
A new login resets the user to Demo. Orders are never retried; an order that fails mid-flight is stored
as `unknown` for review. Errors in one worker never reach another.

## Setup
Deploy as its own service from the repo root with `autotrade/Dockerfile` (e.g. a Render *Docker* web
service, Dockerfile path `./autotrade/Dockerfile`, context `.`), plus a Postgres database. Without Docker:
```bash
pip install -r autotrade/requirements.txt && playwright install --with-deps chromium
python -c "from autotrade.vault.crypto import KeyRing; print(KeyRing.generate_key())"   # -> AUTOTRADE_VAULT_KEY
```
Environment: `DATABASE_URL`, `AUTOTRADE_VAULT_KEY` (or `AUTOTRADE_VAULT_KEYS` + `AUTOTRADE_VAULT_ACTIVE`),
`AUTOTRADE_JWT_SECRET` (32+ chars, shared with your auth), `AUTOTRADE_FEED_SSID` (Demo account),
optional `AUTOTRADE_ALLOW_LIVE`, `AUTOTRADE_MAX_STAKE`, `AUTOTRADE_MAX_WORKERS`, `AUTOTRADE_CORS_ORIGINS`.
Run `AUTOTRADE_APPLY_SCHEMA=1 python -m autotrade.main` once (applies `schema.sql`), then without the flag.
Keep the vault key out of the database and out of git. Run as its own service (needs Chromium and a
long-lived process, so not a serverless function and not the Render GBot service).

GBot (the existing Render service) gets three variables and no new packages:
`AUTOTRADE_API_URL` (URL GBot calls), `AUTOTRADE_PUBLIC_URL` (HTTPS URL users open for login; defaults to the
API URL; must be HTTPS for the in-Telegram button) and `AUTOTRADE_JWT_SECRET` (same value as the service).
The commands are registered in Telegram's menu only when these are set.

## Integrating with your codebase
- Auth: set the same JWT secret, or pass your own dependency: `create_app(rt, auth=your_dependency)`
  returning `Principal(user_id, is_admin)`.
- Frontend: call `/auth/handshake`, draw the JPEG from `/screen`, forward clicks/keys to `/input`, poll
  the handshake state until `done`; then show Demo/Live, settings and `/trading/stats`.
- Database: your own Postgres; run `schema.sql`. On Supabase, the script already revokes `anon`/`authenticated`.
- New strategy: add a class with `required_symbols` and `on_tick` to `trading/strategy.py` and register it in `STRATEGIES`.

## Security notes
- Typed login text passes through the server to the isolated browser; it is never logged or stored.
  Tokens are redacted from logs (`logging_utils`). Do not enable library debug logging.
- The capture browser blocks private/local addresses and limits top-level navigation to login hosts.
  Also run it with no access to your internal network.
- Anyone holding a user's SSID controls their session. Only the vault, the broker adapter and the
  health check ever see plaintext.
- Automated trading and session capture may breach Pocket Option's terms; binary options are
  restricted in some countries. Confirm before offering this to users.

## Verified vs not verified
Verified offline: encryption, risk rules, workers, isolation, reconnect, API, handshake logic with a fake
browser, Postgres schema/queries, GBot's client against the real API app, the Telegram commands, and the
login page in a real Chromium (phone size) against a fake login screen. NOT VERIFIED: the Docker image
build, the real login flow (URLs, allowed hosts, account switching in `auth/handshake.py`), the Telegram
Mini App inside a real Telegram client, Google sign-in (Google may refuse automated browsers), real order placement,
and the error-text matching in `trading/broker.py::map_exception`.

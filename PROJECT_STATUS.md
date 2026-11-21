# G-Bot status — asset-first menu, Auto-Trade setup screen & trade amounts (2026-11-21)

## Fixed / implemented in this revision
- **Main menu**: `/start` shows only **Choose Asset**, **Account Info**, **Support/Help**. The old top-level
  Auto Trading menu (and its `menu:autotrade` screen) is gone; `/autotrade` now answers with the service
  overview. Auto-trading always starts from an asset: Choose Asset → category → asset →
  **📡 Receive Signals** / **🤖 Start Auto-Trade**.
- **🤖 Start Auto-Trade = setup screen, no blind start**: reads the vault (`GET /auth/status`, no browser) for
  the session of the current mode. Not connected → "⚠️ Account not connected" with Log in / 🔑 Direct SSID
  Login and Demo/Real switch. Connected → no login prompt; shows account (🧪 DEMO / 💵 REAL), last known
  balance, trade amount, **🚀 Start Auto-Trade · $X**, Demo/Real switch and the amount picker.
- **Trade amount picker**: $1.00 · $1.50 · $2.00 · $2.50 · $5.00 RISK · $10.00 RISK · $25.00 · $50.00 ·
  $100.00, plus 🛑 Stop (back to signals, stops auto-trade) and 🏠 Main Menu. Saved amount is ticked. Only
  listed amounts are accepted. An amount above the daily stop-loss raises the stop-loss to that amount (the
  user is told). Service caps still apply: Demo ≤ `AUTOTRADE_MAX_STAKE` (100), Real ≤
  `AUTOTRADE_LIVE_MAX_STAKE` (default 10) — higher Real amounts are refused with the service's message.
- **Balance check before start**: 🚀 Start reads a fresh balance; below the trade amount → not started, user
  asked to lower the amount or top up. The per-trade balance check in the worker is unchanged.
- **/at_ssid**: without a value it shows **🧪 Demo Account / 💵 Real Account**; each shows step-by-step
  instructions for that account (`isDemo` 1/0). The SSID can be sent with `/at_ssid` or pasted alone (a
  private message starting `42["auth"`); it is deleted at once. Reply: "✅ Connected to your Pocket Option
  Real/Demo account" when the WebSocket check passed, otherwise "saved, checked again before the first
  trade". Choosing Real but pasting a Demo SSID (or the reverse) is flagged.
- **Setup on every auto-trade message**: opened, result, skipped, low-balance and failed alerts all carry
  `Setup: <name>` (engine codes such as `uptrend_pullback` shown as card names; "Price Action" fallback).
  Signal cards already showed Setup (unchanged).
- Unchanged and re-checked: saved-session reuse before any Chromium launch (`/auth/handshake`, `/at_login`),
  low-memory Chromium flags (`--js-flags=--max-old-space-size=128`, `--no-sandbox`,
  `--disable-dev-shm-usage`, single-process, asset blocking, browser closed right after capture).

## Verified in this revision
- GBot suite: 170 passed (12 new in `tests/test_ui_refinement.py`).
- Autotrade suite: 133 passed, 4 skipped; `test_secure_login` browser test needs Playwright installed
  (not available in the check environment; unchanged code).

---

# G-Bot status — saved-session reuse, Direct SSID login, Real + Demo auto-trade (2026-11-21)

## Fixed / implemented in this revision
- **No repeated browser logins (prevents Pocket Option IP blocks)**: `POST /auth/handshake` first checks the
  vault. If the saved session for the selected mode works, it is reused and **no Chromium is started**
  (`{"reused": true}`; the sign-in page says "Already connected"). A running bot counts as proof; otherwise one
  WebSocket check. Only an explicit broker rejection (`auth_error` → session expired) opens a fresh browser
  login; network trouble keeps the token. "Sign in again anyway" on the page (`force: true`) is the only bypass.
  `/at_start` already connected with the saved SSID over WebSocket only (unchanged), so it survives restarts.
- **/at_login**: when a session is saved it answers "✅ Already connected (Demo ✅ · Real ✅)" instead of
  pushing a new sign-in. The sub-menu now has **🔑 Direct SSID Login**.
- **Direct SSID Login**: `/at_ssid 42["auth",{...}]` (or the button → instructions). The bot deletes the
  message at once; the service (`POST /auth/ssid`, full-scope token, 5/15 min limit) detects Demo/Real from
  `isDemo` (never converted), stores it encrypted, checks it once over WebSocket, and refuses rejected or
  malformed values without echoing them. No browser is used.
- **Demo/Real capture**: `capture_tokens()` waits 3.5 s (`AUTOTRADE_CABINET_SWITCH_DELAY`, default 3.5)
  before opening the other cabinet, so both genuine frames are captured and stored separately.
- **Real + Demo auto-trading**: `AUTOTRADE_ALLOW_LIVE` now defaults to **true**. The asset menu has
  🧪 Demo / 💵 Real / 🔑 Login buttons; 💵 Real asks for one confirmation (button or `/at_live confirm`) and
  needs a saved Real session. A fresh browser login still resets to Demo (Real must be re-confirmed).
- **Balance check before every auto-trade (Demo and Real)**: fresh balance read (last known one if the broker
  is slow). Balance below the stake → trade skipped + "⚠️ Low balance … Top up or lower the stake" alert.
  No balance at all → skipped with an explanation (never trades blind).
- **512 MB**: Chromium JS heap lowered to `--js-flags=--max-old-space-size=128` (plus `--no-sandbox`,
  `--disable-dev-shm-usage`, single-process, asset blocking, close right after capture — unchanged). Session
  reuse means most `/at_login` taps start no browser at all.
- Unchanged: `WELCOME_IMAGE_URL`, Render plan (512 MB), signal card format and the six Setup names,
  max 2 open trades / 5 s cooldown, rate limits.

## Verified in this revision
- GBot suite: 158 tests OK. Autotrade suite: 136 tests OK (3 skipped, need Postgres) — includes new
  `autotrade/tests/test_session_reuse.py` (reuse without browser, unreachable keeps session, auth_error opens
  browser, force, Direct SSID Demo/Real/rejected/malformed/scope, 3.5 s cabinet pause, 128 MB heap, Real
  allowed by default, low-balance skip, balance == stake trades, unreadable balance skips) and new
  `/at_login` / `/at_ssid` tests in `tests/test_autotrade_commands.py`. No test starts a real browser login.

## Not verified
- No new live Pocket Option login was run (on purpose: repeated logins caused the earlier IP block).
- Render memory use under the 128 MB heap flag is not measured on Render itself.
- A real-money (Real account) trade was not placed.

## Needs your action on Render (The-G-Bot-autotrade)
- If `AUTOTRADE_ALLOW_LIVE=false` is set explicitly, change it to `true` (or delete it) — an explicit value
  overrides the new default.
- Optional: `AUTOTRADE_CABINET_SWITCH_DELAY` (default 3.5 seconds).

---

# Previous revision — 512 MB login, Signals vs Auto-Trade, reliability upgrades

## Fixed / implemented in this revision
- **Login on Render 512 MB (no plan change)**: the sign-in browser runs in low-memory mode
  (`--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu --single-process --no-zygote
  --js-flags=--max-old-space-size=256`, low-end-device mode, scale 1, no service workers, one login at a time).
  Pocket Option's images, fonts, media, stylesheets and third-party trackers are not loaded during login
  (Google sign-in / CAPTCHA pages load in full). The browser closes right after the Demo and Real sessions are
  captured; every close step is time-boxed. If Chromium dies (out of memory) the page shows "The secure sign-in
  browser stopped unexpectedly… try again" — never a blank screen. Busy/slow/unreachable cases have their own text.
- Pages the user has to look at load normally: the broker page is stripped only while the user signs in with
  the email/password form. After submitting (verification), after tapping the broker page, and with
  "Open Pocket Option login" / "finish on the Pocket Option page", it loads with styles and pictures.
  Trading cabinet pages (only visited to capture sessions) always stay stripped.
- **Real Demo + Real sessions**: each account's session is now captured from its own Pocket Option page
  (live testing showed a Demo session derived from the Real one is rejected).
- **Menu**: `/start` → category → asset → **📡 Receive Signals** (cards only) or **🤖 Start Auto-Trade**
  (private chats). Auto-Trade users no longer get raw cards; the service sends *Trade opened* and *Trade closed*
  alerts (WIN / LOSS / BREAK EVEN, profit/loss, updated balance) or a short note when a signal is skipped
  (risk limit, payout too low, max open trades). If the auto-trade service can't take a signal, the user gets
  the normal card instead so nothing is lost. New 💰 Balance and 📊 Stats buttons.
- **Who is auto-trading**: the service keeps the list (`GET /trading/active`, service-only token); GBot refreshes
  it at start-up and before signals once the copy is older than 20 s, and updates it instantly on start/stop, and only sends hand-offs to those users.
- **Defaults**: max 2 open trades, 5 s cooldown (old saved defaults upgraded automatically; custom values kept).
- **Balance**: last known balance per user and account is saved in `autotrade_balances` (created at start-up),
  survives restarts. `/at_balance` (+ menu button) reads a fresh balance even when the bot is stopped.
- **Security**: hand-off tokens are `scope=signal` (30 s, only `POST /trading/signal`); GBot's roster token is
  `scope=service`; login links stay `scope=login`. Per-user limits: 6 login starts / 10 min, 5 password
  submissions / 15 min, with a readable "wait N minutes" message.
- **Cleanup**: `gap` setting, `/at_set gap` and the `gap` strategy alias removed; obsolete skipped tests for old
  card formats deleted (`tests/test_live_test_signals.py`, `tests/test_serve.py` and skipped classes elsewhere).
- Unchanged on purpose: `AUTOTRADE_ALLOW_LIVE=false` default, `WELCOME_IMAGE_URL`, Render plan.

## Verified in this revision
- Tests: GBot suite and autotrade suite pass (counts below), including new `tests/test_autotrade_mode.py` and
  `autotrade/tests/test_autotrade_upgrades.py` (low-memory flags, asset blocking, crash message, defaults,
  roster routing, alerts, saved balance, token scopes, rate limits).
- **Live, real Pocket Option Demo account** (test login from Lovable secrets, never stored in the repo):
  sign-in with the low-memory flags and asset blocking succeeded; genuine Demo and Real sessions captured;
  one real Demo order placed — $1 CALL EURUSD_otc, 1 minute, payout 92% — settled LOSS −$1.00, balance
  $10.00 → $9.00 read back from the broker.
- The hosted sign-in page, driven in a real browser against the running service, ends on a readable result
  panel (e.g. "Sign-in failed — Pocket Option did not respond in time") instead of a blank page.

## Not verified
- Memory on a real Render 512 MB instance: browser memory in the test machine is not comparable to Render's
  limit. After deploying, watch Render's memory graph during `/at_login`.
- Pocket Option temporarily stopped answering the test machine after repeated logins, so the full
  service run (hosted page → `/at_start` → signal hand-off → alerts → stats) could not be completed live.
  Logins from Render's shared address may hit the same protection; the new rate limits reduce that risk.
- Docker image with Playwright 1.49 (tested with 1.59 here).

## Environment variables to check on Render
GBot service: `TELEGRAM_BOT_TOKEN`, `PO_SSID`, `TELEGRAM_ADMIN_CHAT_ID` (or `TELEGRAM_CHAT_ID`), `TELEGRAM_ADMIN_ID`,
`DATABASE_URL`, `AUTOTRADE_API_URL` (https URL of the autotrade service), `AUTOTRADE_PUBLIC_URL` (https, optional),
`AUTOTRADE_JWT_SECRET` (32+ chars, identical on both services); optional `ADMIN_IDS`, `ADMIN_TOKEN`, `REFERRAL_LINK`,
`TIMEZONE`, `WELCOME_IMAGE_URL`.
Autotrade service: `DATABASE_URL`, `AUTOTRADE_VAULT_KEY` (base64 of 32 bytes) or `AUTOTRADE_VAULT_KEYS`
(`1:<b64>,2:<b64>`) + `AUTOTRADE_VAULT_ACTIVE`, `AUTOTRADE_JWT_SECRET`, `AUTOTRADE_FEED_SSID` (Demo SSID),
**`TELEGRAM_BOT_TOKEN` (new here — same as GBot; needed for trade alerts)**, `AUTOTRADE_ALLOW_LIVE=false`;
optional `AUTOTRADE_LOW_MEMORY=true`, `AUTOTRADE_MAX_HANDSHAKES=1`, `AUTOTRADE_BLOCK_LOGIN_ASSETS=true`,
`AUTOTRADE_BLOCK_THIRD_PARTY=true`, `AUTOTRADE_HANDSHAKE_RATE=6/600`, `AUTOTRADE_CREDENTIALS_RATE=5/900`,
`AUTOTRADE_TRUST_PROXY=true`, `FORWARDED_ALLOW_IPS=*`, `AUTOTRADE_MAX_STAKE`, `AUTOTRADE_LIVE_MAX_STAKE`,
`AUTOTRADE_MAX_WORKERS`, `AUTOTRADE_CORS_ORIGINS`. Remove any old `gap` setting references (none are env vars).

---

# G-Bot status — 2026-11-21 (login, setup cards, signal auto-trade)

## Fixed / implemented in this revision
- **Hosted login submission (`/at_login` → `/login`)**: Render terminates TLS at its proxy, so the service saw
  plain `http` and refused every email/password submission ("HTTPS is required"). The service now honours the
  proxy's `X-Forwarded-Proto` (uvicorn `proxy_headers`, `FORWARDED_ALLOW_IPS`, `AUTOTRADE_TRUST_PROXY`, default on).
  Plain HTTP from the internet is still refused, and never echoes the password.
- Credential submission never returns a bare 500: browser/page failures become a 409 with a fixed, safe message.
- The isolated sign-in browser waits for the broker's first response (`commit`) instead of the full page load,
  so slow Pocket Option pages no longer fail the login; open failures are reported as `broker_unreachable` or
  `browser_unavailable` with a user-facing `message` in `GET /auth/handshake/{id}`.
- Login page: every outcome ends on a visible panel (Account connected / Sign-in failed / timed out / cancelled /
  link expired) with **Try again** and **Return to Telegram**; Pocket Option's own error text (wrong password,
  verification) is shown after submitting. No blank screens.
- Verified Pocket Option's live mobile login form (redirects to `m.pocketoption.com`): one `type=email name=email`
  field and one password field inside the form — matches the submission selectors.
- **Signal cards** now show the market setup:
  `🟢 BUY / CALL` or `🔴 SELL / PUT` · Setup · Asset · Expiration 1 minute · Payout · `Payouts may vary.`
  Setup names: Uptrend Continuation, Uptrend Pullback, Consolidation High Rejection, Consolidation Low Rejection,
  Downtrend Continuation, Downtrend Pullback.
- **Auto-trade follows GBot signals**: every signal delivered to a private subscriber is handed to the autotrade
  service (`POST /trading/signal`, in parallel with the Telegram cards). A user's running bot places it on their
  own Demo/Real account through the existing risk manager and payout check. Duplicates and expired signals are
  ignored; group chats never trade. New setting `follow_signals` (default on; `/at_set follow off` returns to the
  service's own price-action engine on `symbol_otc`).
- **Results and balance**: wins, losses, break-even (draws) and P/L are recorded per trade; the connected account's
  balance (Demo or Real) is read after connecting and after every settled trade. `/autotrade` and `/at_stats` show
  balance, wins, losses, break-even, P/L and win rate.
- `autotrade/requirements.txt` pins Playwright to the Dockerfile base image version (1.49.0) so Chromium matches.

## Verified in this revision — offline / local only
- Full suite: 229 passed, 33 skipped (legacy + PostgreSQL integration without `AUTOTRADE_TEST_DATABASE_URL`).
- New tests (`autotrade/tests/test_signal_follow.py`, `tests/test_signal_card.py`): signal placement on the
  user's account, CALL/PUT mapping, duplicate/expired/not-running/follow-off handling, win/draw recording,
  balance in stats, signal endpoint auth/validation, proxy HTTPS acceptance, plain-HTTP refusal, unreachable
  broker message, exact card layout, the six setup names, hand-off to private chats only, stats text.
- Browser check of the hosted page with a simulated broker: successful sign-in ends on "Account connected"
  with the password field cleared; an unreachable broker ends on "Sign-in failed" with a working Try again.

## Not live verified
- A real Pocket Option sign-in (real credentials, CAPTCHA/2FA), real orders, real balance reads and the deployed
  Render services were not exercised from this environment.
- Deploy both services, then: `/at_login` → sign in → `/at_start` (Demo) → wait for a signal on your selected
  asset → `/at_stats` should show the trade, result and balance.

## Environment variables
- GBot (Render `gbot`): `TELEGRAM_BOT_TOKEN`, `PO_SSID`, `TELEGRAM_ADMIN_CHAT_ID` (or `TELEGRAM_CHAT_ID`),
  `TELEGRAM_ADMIN_ID`, `ADMIN_IDS`, `DATABASE_URL` (subscribers), optional `ADMIN_TOKEN`, `REFERRAL_LINK`, `WELCOME_IMAGE_URL`,
  `TIMEZONE`; for auto-trading `AUTOTRADE_API_URL`, `AUTOTRADE_PUBLIC_URL` (HTTPS), `AUTOTRADE_JWT_SECRET`.
- Autotrade service: `DATABASE_URL`, `AUTOTRADE_VAULT_KEY` (or `AUTOTRADE_VAULT_KEYS` + `AUTOTRADE_VAULT_ACTIVE`),
  `AUTOTRADE_JWT_SECRET` (same value as GBot), `AUTOTRADE_FEED_SSID`, optional `AUTOTRADE_ALLOW_LIVE`,
  `AUTOTRADE_LIVE_MAX_STAKE`, `AUTOTRADE_MAX_STAKE`, `AUTOTRADE_MAX_WORKERS`, `AUTOTRADE_CORS_ORIGINS`,
  `AUTOTRADE_TRUST_PROXY` (default true), `FORWARDED_ALLOW_IPS` (default `*`), `AUTOTRADE_APPLY_SCHEMA=1` once.
  No new secrets are required; no database schema change (settings are JSON).

---

# G-Bot refinement status — 2026-11-21

## Implemented in this revision
- `/start` sends the supplied futuristic trader header with a concise caption. `WELCOME_IMAGE_URL` can override the hosted image; a failed image request falls back to text.
- Main menu: Choose Asset, Account Info, Auto Trading, Support/Help. Submenus edit the existing text/caption, include Back, and do not modify independent signal messages.
- Public Telegram command scope contains only `/start`. All eight administrative commands are guarded by `ADMIN_IDS` and the configured admin-chat boundary; admin actions are absent from public menus.
- Asset categories re-check the broker's active-asset response; closed, unmapped, and known zero-payout assets are omitted. A failed availability request does not expose stale cached assets.
- Candle builders accept only 60-second intervals. A broker-timestamp/monotonic clock drives scheduled minute closes without waiting for a next-minute tick. Partial startup candles are excluded; late closes cannot trigger fresh signals.
- Consolidation SELL requires a bearish closed candle near range high; BUY requires a bullish closed candle near range low. Existing trend continuation/pullback logic is retained.
- Removed executable legacy indicator engines and indicator-only strategy modules. ATR remains a price-derived range measurement; no oscillator/Bollinger strategy executes.
- Auto-trading uses the same price-action engine, timestamped feed ticks, minute-boundary wake-ups, and 60-second expiry. Saved `gap` settings are a compatibility alias to price action, not the former gap strategy.
- Independent HTML signal cards show BUY/CALL or SELL/PUT, asset, one-minute expiration, live payout (or unavailable), and only `Payouts may vary.` Telegram supports bold text and colored emoji, not arbitrary font sizes or colored text.
- Existing database structures, encrypted broker-session vault, private hosted login, heartbeat/reconnect checks, risk limits, and open-trade recovery remain intact. CSV stores preserve existing headers/rows.
- Hosted `/login` now presents a standard email/password form. A validated, owner-authenticated submission fills and submits the existing HTTPS broker form in an isolated browser; no separate broker keys or OAuth application are required for this path.
- Broker cookies and account tokens are stored together in a versioned AES-256-GCM encrypted payload inside existing credential rows. Legacy token-only rows remain readable; no database schema changes or additional profiles are introduced. Google-domain cookies, passwords, and unrelated browser state are excluded.
- Passwords are transient, never persisted or returned by API responses, and cleared after submission. Invalid request responses omit rejected values; nonlocal plain HTTP credential submission is rejected. CAPTCHA/2FA remain on the interactive broker page.

## Verified in this revision — offline only
- Bot suite: 163 tests run, 133 passed, 30 previously skipped legacy tests, no failures.
- Auto-trading suite: 83 tests run, 80 passed, 3 skipped PostgreSQL integration tests, no failures.
- Eight new secure-login tests cover form submission, session-owner/bearer checks, password-safe validation/errors, HTTPS enforcement, cookie encryption and per-user isolation, legacy compatibility, Google-cookie exclusion, and disconnect deletion.
- Browser end-to-end check: actual hosted form → isolated Chromium broker-page simulation → account-token/cookie capture → two encrypted account rows → connected status; cleared password input, no browser errors, no phone-width overflow. Fake credentials and broker responses only.
- New concrete tests cover closed-minute timing at :00, first next-minute tick after :00, rejected 15/35-second candles, required 60-second expiry, bearish/bullish range confirmation, monotonic broker clock, scheduled auto-trading decisions, and administrator IDs.
- Existing regression coverage includes single-menu behavior, broker-symbol filtering, Telegram delivery/rate-limit handling, login-link permissions, encrypted session handling, reconnection, demo/live boundaries, and risk enforcement.
- Order/risk tests use a clearly test-only scripted decision provider; production price action is verified separately. No real orders, real credentials, paid AI calls, or production database writes were used.

## Not implemented / not live verified
- Direct password collection in Telegram chat is not added: use the hosted email/password form instead. Its real broker page, credential selectors, CAPTCHA/2FA and resulting live account sessions still need deployment verification.
- Live PostgreSQL persistence has not been rerun: no `AUTOTRADE_TEST_DATABASE_URL` is configured. Offline tests verify encrypted payload persistence through the repository interface and vault recreation, not a production database.
- A dedicated Google OAuth flow requires documented broker-supported authorization endpoints, client registration, and redirect approval. A Google button in the existing broker page is not a verified SSO integration.
- Multi-hour live session continuity, broker master-clock accuracy/network latency, live payouts/signals, real Telegram photo/submenu behavior, and the autotrade Docker image have not been verified on the deployed services. Tick-derived synchronization is not an independent server-time API check or a hard real-time guarantee.
- The Python bot still runs on its separate Python host; the Lovable page is a repository status view, not an operating Telegram bot or trading terminal.
- Deploy both Python services with their existing environment configuration and test the Telegram menu plus broker DEMO login first. Use a stable HTTPS `WELCOME_IMAGE_URL` if changing hosting.

The sections below are the repository's prior report, retained as historical context; their earlier live checks do not verify this revision.

---

# Historical baseline — 2026-11-20

## Goal
- Read-only Pocket Option signals on Telegram: every listed asset selectable, payouts
  read live from the feed, one menu message per chat. The signal scanner never places trades.
- Optional multi-user auto-trading runs in the separate `autotrade/` service; GBot only controls it
  with /autotrade commands, and only when AUTOTRADE_API_URL + AUTOTRADE_JWT_SECRET are set.

## Implemented
- Pinned BinaryOptionsToolsV2 0.2.18 (multi-symbol streaming fix) — pyproject.toml, uv.lock.
- Exact broker symbol per catalogue asset, name fallback — assets.json, market_assets.py.
- Subscribe on select, 2 x 10 s tick wait, MAX_SUBSCRIBED=30, 10-min unsubscribe — scanner.py.
- History requested in server time (UTC+2), returned as UTC — broker/pocket_option.py.
- Tick timestamps normalised for both library formats — data/candles.py.
- First live candle never partial; open-minute history completes it — data/candles.py.
- Payout read from the feed at display/signal time, never stored — scanner.py, telegram_bot.py.
- DEMO/REAL from the SSID in /status, /admin and admin JSON — config.py, telegram_bot.py, main.py.
- Signal shows "Payout: N%" or "Payout: unavailable" plus the payout disclaimer — notifications.py.
- One menu per chat: edit in place, /start deletes the old menu, failed edit sends one new — telegram_bot.py.
- Selection confirmation with Change asset / Stop; every callback answered first — telegram_bot.py.
- Asset selection runs in the background so polling isn't blocked — telegram_bot.py, main.py.
- CLI: --run availability, payouts, candles <asset>, asset-check <asset> — main.py.
- autotrade service (separate, off until deployed): login capture in an isolated browser, AES-256-GCM
  token vault + session health check, one shared price feed, one worker per user, risk limits,
  Demo by default, FastAPI /trading/* endpoints — autotrade/ (see autotrade/README.md).
- Telegram auto-trading commands: /autotrade, /at_login (15-min login-only Mini App link), /at_start,
  /at_stop, /at_stats, /at_set, /at_live confirm, /at_demo, /at_logout; admin /at_halt, /at_resume.
  Private chat only; slow calls run in the background — autotrade_client.py, autotrade_commands.py.
- Login page served by the service at /login (token in URL fragment, login token can't trade) —
  autotrade/api/login_page.html. Shows Pocket Option's own mobile login page full screen, rendered at
  the user's phone size; tapping the site's email/password field opens the phone's own keyboard
  (native input over the field, autofill works); Google button tappable; drag to scroll. No separate
  "type here" box any more.

## Verified live (DEMO SSID, read-only, 2026-11-20 07:05–07:20 UTC)
- availability: 40/56 catalogue assets ticking within 10 s (was 3/56 on 0.2.15).
- asset-check AUD/CAD, CAD/JPY OTC, Solana OTC, GBP/USD OTC: all available on attempt 1.
- Six sequential selections on one connection all streaming together (production bug fixed).
- candles GBP/USD OTC: closed 1-minute candles on whole minutes; prices match the live stream.
- payouts: all 40 mapped assets return a feed payout (values changed between runs).
- serve 3 min (Telegram polling OFF, JSON storage): startup message sent, /health ok.

## Mock-only / offline
- Full suite: 187 tests, 144 pass, 43 skipped as OBSOLETE, 0 fail (17 new auto-trading command tests).
- autotrade suite: 77 tests, 74 pass, 3 skipped (Postgres tests need AUTOTRADE_TEST_DATABASE_URL), incl. GBot's
  client against the real API app and login-token limits.
- Login page in headless Chromium at phone size against a fake login screen: tapping the email and
  password fields focused a native input, typing filled the site's fields, Google button tap reached
  the page, password never returned by /view. NOT VERIFIED on the real Pocket Option page (not
  reachable from the test machine on 2026-11-20), Google pop-up, real Telegram client.
- Re-check 2026-11-20 12:30 UTC on commit 3400281: GBot suite 144 pass / 43 skipped / 0 fail; autotrade
  suite 74 pass / 3 skipped (Postgres) / 0 fail, incl. the phone-login browser test. Real Pocket Option
  login page again unreachable from the test machine (plain browser timed out too, so it is the network,
  not the login code) — first real test must run from the deployed autotrade server.
- Skipped tests were already failing before today and test removed features (old CLI,
  15 s candles, paper tracker, strength ranking, /start-subscribes flow); reason is on each.
- Strategy engine unchanged; 27 strategy tests pass, incl. new-high -> pullback SELL ->
  candle holding the pullback low -> BUY (never SELL). Fix was test fixtures only
  (30-candle warm-up did not apply to short state-machine fixtures).
- Menu tests: 200 random taps keep one menu; signals never edited or deleted.

## Known issues
- 15-16 catalogue names are not listed by the broker (e.g. AED/CNY non-OTC, USD/BDT,
  EUR/NZD non-OTC); they show "isn't available right now". Fix: remove or rename them
  in assets.json once you confirm the names you want (Step 3 follow-up).
- Feed history ends about 3-4 closed candles behind the clock; the strategy sees that
  small gap once after a selection. Fix: optional re-seed 5 min after selection (Step 6).
- Menu message IDs are kept in memory; after a Render restart the first /start cannot
  delete the pre-restart menu. Fix: store menu_message_id with the subscriber (Step 5).
- Before this fix, live history was 2 h stale and ticks used +2 h labels (0.2.15);
  signals produced by the old deploy were seeded from 2-hour-old candles (Step 2/3).
- In-bot tap test (/start, Currencies, asset, Back) NOT RUN: needs a real Telegram user
  and the new code deployed; serve polling was kept off to avoid clashing with Render.

- Auto-trading NOT VERIFIED live: real Pocket Option login and SSID capture, Google sign-in (Google may
  block automated browsers), real orders, the Docker image build, and the Mini App in a real Telegram app.
- Risk: Pocket Option may block data-centre addresses (it blocked the test machine). If /at_login shows a
  blank or timed-out page on the deployed server, the autotrade host's network is being refused.

## Needs your action
- To use auto-trading: deploy autotrade/Dockerfile as its own service with Postgres and its env vars
  (autotrade/README.md "Setup"), then set AUTOTRADE_API_URL, AUTOTRADE_PUBLIC_URL (HTTPS) and the same
  AUTOTRADE_JWT_SECRET on the GBot Render service. Test with a Demo account first.
Push this update to the GitHub repository Render deploys from (Render rebuilds with
`uv sync --locked`); then in Telegram tap /start, Currencies, an asset, Back and confirm one menu.

## Next step
- After deploy: run the in-bot tap check and /admin, then decide on the unlisted names.

## Last run date
- 2026-11-20 UTC.

# Oracle Ubuntu VPS production refactor

## Scope and verification boundary

The Telegram bot, broker login browser, and auto-trading engine remain on your existing Oracle Cloud Always Free Ubuntu VPS. Two supervised Python processes run on that one host. The bot calls the engine over loopback; only the HTTPS login endpoint is public. Telegram uses long polling, not hosted-service webhooks.

These changes were verified with offline broker/database doubles and local browser regression tests, not your VPS, production Telegram account, Neon database, or real broker sessions. Do not treat offline success as proof of production readiness. Test Demo first; Real trading remains opt-in and can lose money.

## Changes

- Removed the obsolete hosted-web-service deployment manifest and obsolete hosting test URLs.
- Both processes load the repository `.env` using python-dotenv, regardless of working directory. Existing service environment variables take precedence; no platform-specific secret loader is used.
- Scanner: `PO_SSID`, then `AUTOTRADE_FEED_SSID`. Engine feed: `AUTOTRADE_FEED_SSID`, then `PO_SSID`. Values are trimmed. A dedicated Demo feed is recommended.
- Internal API defaults to `http://127.0.0.1:8100`. `AUTOTRADE_PUBLIC_URL` is your actual public HTTPS URL; a loopback URL cannot work on a user's phone. Do not invent a domain or expose plain HTTP login publicly.
- Engine bind: `AUTOTRADE_HOST=127.0.0.1`, `AUTOTRADE_PORT=8100`. Bot health bind: `HOST=127.0.0.1`, `PORT=5000`. Separate port names avoid collisions from one shared `.env`.
- Trusted Uvicorn proxy IPs default to loopback. Raw forwarded-header trust is off (`AUTOTRADE_TRUST_PROXY=0`). The sample local proxy supplies and replaces forwarding headers.
- Normal Chromium behavior replaces constrained-host single-process/asset-stripping defaults. Optional low-memory flags and bounded browser concurrency remain available.
- Both broker adapters use the native asynchronous initialization factory for pinned BinaryOptionsToolsV2 0.2.18. Cancellation/timeouts clean up late connections instead of blocking unrelated tasks.
- Balance connections use per-account ownership with bounded capacity and deadlines. Failed worker adoption retains socket ownership; in-use sockets are not evicted.
- Feed connection cancellation closes unpublished sockets; a stale stream cannot close a newer replacement connection. Browser launch spacing is serialized.
- Worker-start reservations prevent concurrent users from exceeding capacity.
- Neon queries use transaction-local statement/lock/idle-transaction limits when the server has no nonzero limit. Existing TLS, keepalives, checkout limits, read retries, and no blind write retries are preserved.
- Existing background Telegram command dispatch is retained and tested rather than replaced with unbounded parallel polling.

Historical audit/status files and unused development-tooling references are snapshots, not current runtime requirements. See root `AGENTS.md` for the runtime boundary.

## Configuration on the existing VPS

Preserve your existing `.env`, vault key, database URL (including TLS settings), and sessions. Do not replace the populated `.env` with the example. Add the missing settings from `.env.example` securely on the VPS and restrict file access (`chmod 600 .env`). Lovable Secrets do **not** automatically update an external VPS or its systemd environment.

Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, existing Neon `DATABASE_URL`, at least one feed SSID, `AUTOTRADE_JWT_SECRET` (32+ characters shared by both processes), and the **existing** `AUTOTRADE_VAULT_KEY` or existing rotating-key settings. Use your actual `AUTOTRADE_PUBLIC_URL=https://...` served by a valid certificate.

`AUTOTRADE_ALLOW_LIVE=1` enables eligibility for Real trading. Users still start in Demo, need an active Real session, and must explicitly confirm Live. Existing payout, balance, exposure, stake, and operator-halt checks remain. An explicit `AUTOTRADE_ALLOW_LIVE=0` still disables Live.

Optional query limits (milliseconds): `AUTOTRADE_DB_STATEMENT_TIMEOUT_MS=15000`, `AUTOTRADE_DB_LOCK_TIMEOUT_MS=5000`, `AUTOTRADE_DB_IDLE_TRANSACTION_TIMEOUT_MS=30000`. Invalid negative/non-numeric values fail before connecting. Nonzero server/DSN limits take precedence.

Do not increase `AUTOTRADE_MAX_HANDSHAKES` just because hosting changed. Measure the VPS's memory under concurrent trading and login first. On an ARM VPS, confirm that your Python version/architecture has a compatible pinned broker wheel and Chromium; the x86 offline tests do not verify ARM packaging.

## Apply the delivered patch to GitHub

The patch is based on commit `3f199220b9dcecee0052105e588e7b718fb88979` of `https://github.com/freshforex164-cmyk/The-G-Bot`. Direct commit/push from the Lovable sandbox is prohibited. These commands are for your authorized workstation, **not** commands already performed:

```bash
git status --short
git rev-parse HEAD
# Confirm a clean working tree and review changes since the base commit.
git apply --check /path/to/oracle-vps-refactor.patch
git apply /path/to/oracle-vps-refactor.patch
python -m pip install -e . -r autotrade/requirements.txt
python -m unittest discover -s tests -t .
python -m unittest discover -s autotrade/tests -t .
git add .env.example AGENTS.md runtime_env.py config.py main.py autotrade_client.py \
  autotrade_commands.py pyproject.toml uv.lock broker autotrade tests deploy docs render.yaml
git commit -m "Refactor bot and autotrade engine for single-host Oracle VPS"
git push origin main
```

If `git apply --check` fails, do not force the patch or overwrite the newer repository. Reconcile against the latest revision. The full source ZIP is a review/reference copy; do not extract it over an existing `.git`, populated `.env`, or runtime data.

## Install or adapt supervision

Use your existing supervisor where possible. The supplied systemd units assume the service user/group `gbot`, repository `/opt/The-G-Bot`, and virtual environment `/opt/The-G-Bot/.venv`. Adapt these paths/user to your **existing** installation before copying units. Never start a second Telegram poller alongside the existing one.

On the VPS, pull the pushed revision into a clean checkout and install dependencies in its virtual environment:

```bash
git pull --ff-only
.venv/bin/python -m pip install -e . -r autotrade/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

Use the supported Python version (3.12+). Installing Chromium OS dependencies requires appropriate administrator permissions on Ubuntu. Preserve existing vault keys and the database; no new application schema is introduced by this refactor. On a fresh database only, the existing `AUTOTRADE_APPLY_SCHEMA=1` startup flag applies the existing schema.

Review `deploy/nginx.conf.example`, replacing its example domain/certificate paths with your existing HTTPS configuration. Keep ports 8100/5000 private in both the Ubuntu firewall and Oracle network rules. Validate the proxy configuration before reloading it. If you use another trusted proxy, explicitly configure its IPs; never set `FORWARDED_ALLOW_IPS=*` on a publicly reachable engine.

For systemd installations, install the reviewed units and reload the daemon. Stop/disable the previous supervisor's bot and engine before enabling these units. During a maintenance window with **no open trades**, restart the bot and engine; do not reset or kill active orders merely to deploy.

```bash
sudo systemctl daemon-reload
sudo systemctl enable gbot-autotrade gbot
sudo systemctl restart gbot-autotrade gbot
sudo systemctl status gbot-autotrade gbot --no-pager
```

The units use automatic failure restart, network ordering, restricted environment-file permissions, and a graceful shutdown window. The bot's scanner and Telegram polling retain separate asyncio tasks; the auto-trading process remains independent.

## VPS acceptance checklist (not yet performed)

1. Confirm exactly one bot poller and one engine process. Existing webhooks must be absent before polling; remove a stale webhook through your secure Telegram administration path, not by printing the bot token.
2. Confirm loopback `/healthz` responds and public `/login` loads with a valid HTTPS certificate. Do not print token fragments, sessions, passwords, or connection strings in diagnostics.
3. Complete secure broker login from Telegram, including any required CAPTCHA/2FA. Confirm both saved Demo and Real slots, and verify their balances belong to the expected account. CAPTCHA bypass is not part of this refactor.
4. Start Demo auto-trading with a valid, available asset and your intended stake. Observe a new signal, broker order, settlement, saved result, balance readback, and Telegram alerts. Confirm payout/market-unavailable signals are skipped rather than forced.
5. While one login/balance request is slow, confirm another user's commands, feed processing, and running trades remain responsive.
6. Confirm transient broker/Neon failures recover, rejected sessions prompt re-login, and no ambiguous trade/order is blindly replayed.
7. Only if you intentionally authorize real-money testing, explicitly confirm Live with a small stake within your limits. Confirm the Real account, order, settlement, and persistence. Do not perform Real acceptance testing automatically.
8. Inspect sanitized supervisor logs and check memory, CPU, connection counts, and overnight behavior before declaring production-ready.

Rollback: keep the previously deployed revision available; during a maintenance window with no open trades, restore that revision, reinstall its dependencies, and restart the same supervisor. Preserve `.env`, vault keys, database, and runtime data. No data migration rollback is needed for this patch.

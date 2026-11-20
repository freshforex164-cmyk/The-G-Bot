# GBot baseline audit — 19 November 2026

## Scope and provenance

- Audited public GitHub `freshforex164-cmyk/The-G-Bot`, branch `main`, commit `8808f4526deea24ab2dc5009de0e81e212644513` (18 November 2026). GitHub API confirmed this head; source downloaded to temporary storage, not imported over the Lovable app.
- Read `PROJECT_STATUS.md` first. It is dated 24 October and contradicts current settings and test behavior.
- No bot source, Render configuration, health endpoint, strategy settings, or production service was changed. No production Telegram poller was started. No secrets were printed.
- Render deployment passing is owner-reported, not independently checked here.

## Checkpoint 1 — actual baseline

Python 3.13.18; isolated temporary dependencies: pinned BinaryOptionsToolsV2 0.2.15 and psycopg 3.3.9. Credentials excluded from the test environment. Python socket connections restricted to loopback for HTTP tests. No provider account was connected.

Full offline discovery: **141 tests run; 17 failures; 41 reported errors; 0 skipped.** This is NOT a passing suite. Error count includes subtest errors and failed module imports; do not subtract these counts to invent a pass count. Additional tests behind failed module imports did not run. Full transcript: `/tmp/gbot-full-baseline.log` in this audit session.

Focused results:

| Group | Result |
|---|---|
| Price-action classification/configuration | 7/7 pass |
| Strategy setups | 10 run: 2 pass, 2 failures, 6 errors |
| Exact-name asset matching | 5/5 pass |
| Signal outcomes | 4/4 pass |
| HTTP health/admin behavior, loopback only | 3/3 pass |
| Decision storage | 1/1 pass |
| Legacy paper storage | 2 run, 1 failure (expects 35 seconds; current value 60) |

**BUY regression:** with 30 history candles and a mocked UPTREND classifier, the shipped engine emitted pullback SELL followed by BUY for a red candle whose low stayed above the tracked pullback low. Concrete comparison: tracked low 95, following low 96. PASS as a targeted offline diagnostic; NOT real-classifier or live-chart acceptance. Engine unchanged.

**Setup-test defect:** `strategy/price_action.py:254–255` requires 30 candles, while `tests/test_strategy_setups.py` supplies 2–6. Temporarily lowering only the in-memory test instance's minimum made all 10 setup tests pass. This isolates a fixture defect; it does not fix the shipped suite or justify changing production thresholds.

Other baseline errors include removed legacy API references (`ReplayVariant`, `main.PaperTracker`), old signal fixtures missing `setup`, and obsolete notification queue constructor arguments. The subscribe-before-history test also fails with `AssertionError: unexpectedly None`; its root cause needs separate analysis before calling this a production mapping defect.

## Checkpoint 2 — availability findings

- Catalog has 56 assets across all five requested categories, with separate OTC/non-OTC identities (`assets.json`, `market_assets.py:34–52`).
- Matching uses exact provider display name; only trims whitespace (`market_assets.py:29–31,65–80`). No catalog aliases are supplied. Provider naming differences can therefore prevent a match.
- Once matched, code retains the provider's actual symbol rather than synthesizing an OTC suffix (`market_assets.py:88–98`). **Wrong suffix is a hypothesis, not confirmed cause.**
- Pinned library documents `EURUSD_otc`, accepts exact symbol in `subscribe_symbol(asset)`, and supports `payout(asset)` in `BinaryOptionsToolsV2/pocketoption/asynchronous.py:1086–1134,1280–1285`. Documentation is not a substitute for the connected account's live list.
- New selection currently does one short-lived probe, closes it, then seeds history before opening the lasting stream (`scanner.py:151–186,262–272,274–304`). Wait/freshness limits are five seconds, not requested two ten-second attempts.
- Existing unused-stream cleanup has a 600-second grace period (`scanner.py:229–261`), but actual broker unsubscribe coverage requires testing. No requested default-30 subscribed-symbol admission limit was identified.
- Startup callbacks may arrive before broker readiness; missing match, closed flag, stale timestamp, delayed tick and provider errors all collapse to the same unavailable message.
- Solana OTC is present in the current catalog (`assets.json:55`); its live match and first-tick behavior remain unverified.

**Per-asset live table: NOT RUN.** No Pocket Option session is configured in this Lovable project. Cannot honestly report live-list presence, subscriptions or last-ten-second ticks for AUD/CAD, CAD/JPY OTC, Solana OTC, GBP/USD OTC or the full catalog.

## Checkpoint 4 — payout findings

- Broker exposes live payout retrieval, but signal construction uses retained asset metadata (`scanner.py:123–135,363–369`). Metadata row payout takes precedence over the payout dictionary (`market_assets.py:81`).
- Missing payout is omitted rather than `Payout: unavailable` (`notifications.py:34–40`). Menu buttons likewise use retained values.
- A demo-mode accessor exists in the broker adapter; requested account label and full payout disclosure are not established in the current messages.
- Fresh exact-symbol payout at display/signal time and `--run payouts` still need implementation and offline/live verification.

## Checkpoint 5 — menu findings

- Confirmed: menu navigation calls `send_message`; no retained menu message ID or edit/delete transport exists (`telegram_bot.py:234–263,490–565`; client `157–170`). This explains menu stacking at the code level.
- Signals use a separate send path (`telegram_bot.py:794–841`) and currently are not edited/deleted.
- Needed: track menu IDs, edit start/category/asset/back/error views, delete previous tracked menu on /start, replace failed edits safely, answer callbacks, and test that signal IDs never become menu targets.
- If an edit fails but the old menu still exists, simply sending a replacement can leave two menus. Replacement must attempt cleanup of the tracked old menu; inability to delete must be recorded, not claimed as “one menu”.

## Checkpoints 6–7 — timing and strategy

- Current settings enforce 60-second candles and expiry, not the status file's 15/35 (`strategy.json`, `strategy/settings.py:38–39`). Preserve them.
- **Partial first candle reproduced:** start tick 30 seconds into a minute, next-minute tick returns that incomplete candle. `data/candles.py:124–149` does not reject the startup partial bucket. This violates the requested first-candle check, without requiring a timeframe change.
- Candle close detection depends on the next bucket's tick. Exact alignment against the chart: NOT RUN.
- Active strategy uses ATR to classify consolidation (`strategy/price_action.py:108–130`), despite the earlier “no indicators” brief. Latest instruction says preserve the engine except failing behavior; do not remove ATR as an unsolicited change.
- Configured validity is 15 seconds while signals/queue advertise or enforce 10; record discrepancy before any scoped correction.
- Historical level deduplication and pending outcomes are in memory; outcome summaries mix LIVE and PROXY and can settle late. These are audit findings, not authorized extra repairs in the current availability/payout/menu scope.

## Deployment and safety

- Both `pyproject.toml` and `uv.lock` exist; pinned broker dependency and Render start/build commands align. No Reflex runtime dependency found. Do not restore files unnecessarily.
- No first-party order placement, password collection or runtime AI call found. Read-only behavior is enforced by application usage, not proof that the external trading-capable library has restricted account permissions.
- `/admin/status` is token protected; `/health` is liveness rather than feed readiness. Both remain unchanged.
- Subscriber JSON and signal CSV durability on Render are not proven; no production storage changes requested.
- Admin text commands check sender and chat; live command-menu visibility is NOT RUN.

## Live checks and delivery

All requested provider CLI checks, chart comparison, three-minute serve, real startup message, deployed /health response and Telegram navigation checks: **NOT RUN**. Only loopback health tests were run. Production configuration is not present; a separate dev bot token is also required before any polling test.

Checkpoint commit/push: **NOT RUN**. Lovable's project checkout is a separate template, not the bot's existing GitHub working tree; copying files here does not update that repository. Stateful Git commands are unavailable under this environment's rules. No code migration or production repair is claimed.

## Needs your action

Keep the running Render bot unchanged. Provide safe live diagnostic outputs from that service or authorize a separate, securely configured read-only test environment; never paste secrets into chat. Updating the existing bot repository also requires an agreed source delivery/Git workflow.

## Next checkpoint

Complete the real live-symbol diagnosis before claiming an availability fix, then repair fixtures and the scoped subscription, payout and menu paths with tests. Mark everything not exercised as NOT RUN.
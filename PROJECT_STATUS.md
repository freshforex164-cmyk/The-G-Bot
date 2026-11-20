# GBot status — 2026-11-20

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

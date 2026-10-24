# GBot status

## Goal
GBot scans Pocket Option OTC pairs on 15-second candles with a Stochastic + Bollinger + SMA strategy.
It sends signals to Telegram subscribers and logs every decision and outcome for measurement; users place trades themselves.

## Core idea
- Read market data and evaluate completed candles only.
- Notify subscribers; never place trades.
- Keep a durable record of decisions and outcomes.

## Done
- Step 0: Read this handoff and `PROJECT_CONTEXT.md`; checked current code and tests. GBot remains read-only.
- Step 1: `run_feed_check` returns `ok`, `tick_count`, `last_price`, and `reason`; CLI exits non-zero on failure. Full offline suite: 97 tests passed on 2026-10-24.
- Existing live-feed result in the prior status was historical and has not yet been rechecked in this run.

## Not done
- Steps 2–7: Live feed recheck, current Telegram/serve verification, decision log, current-rule replay, final checkpoint.

## Known walls with fallback
- Live Pocket Option and Telegram behavior is UNVERIFIED in this run; use offline mocks where possible.
- Telegram `getUpdates` refusal and `chat not found` were not encountered; no live Telegram send/poll was run.
- Current candle configuration is 15 seconds; some replay output text still says 10 seconds and needs correction.
- `ADMIN_TOKEN` must be configured before `/admin/status` can be used.
- GitHub push availability is UNVERIFIED; preserve local commits if remote push fails.

## Needs your action
- Later: set `ADMIN_TOKEN` through Replit Secrets before using the protected web endpoint.

## Next step
- Step 2: Recheck `--run feed-check` using the configured secret; stop live checks if authentication is rejected.

## Last run date
- 2026-10-24; offline suite: 97 passed. Live feed: NOT RUN in this pass yet.

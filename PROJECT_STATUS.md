# GBot status

## Goal
GBot scans Pocket Option OTC pairs on 15-second candles with a Stochastic + Bollinger + SMA strategy.
It sends signals to Telegram subscribers and logs every decision and outcome for measurement; users place trades themselves.

## Core idea
- Read market data and evaluate completed candles only.
- Notify subscribers; never place trades.
- Keep a durable record of decisions and outcomes.

## Done
- Step 0: Read `PROJECT_CONTEXT.md`. Initial code inspection finds read-only scanning, Telegram commands, JSON/Postgres subscriber storage, and an approximate replay command; these are UNVERIFIED at runtime.
- Step 1: Feed check returns `ok`, `tick_count`, `last_price`, and `reason`; CLI returns non-zero on failure. Full offline suite: 89 tests passed.

## Not done
- Step 2: Bounded live feed check and result.
- Steps 3–7: Verify/build commands, admin and serve endpoints, decision log, replay report, final checkpoint.

## Known walls with fallback
- Live Pocket Option and Telegram behavior is UNVERIFIED; use offline mocks where possible.
- Current candle configuration is 15 seconds; some replay output text still says 10 seconds and needs correction.
- `ADMIN_TOKEN` is not among the configured secrets; `/admin/status` will require it before live use.
- GitHub push availability is UNVERIFIED; preserve local commits if remote push fails.

## Needs your action
- Later: set `ADMIN_TOKEN` through Replit Secrets before using the protected web endpoint.

## Next step
- Step 2: Run the bounded live feed check once; apply only the requested stall recovery if needed.

## Last run date
- 2026-10-24; offline suite: 89 passed. Live connections not run.

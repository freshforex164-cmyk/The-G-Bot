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

## Not done
- Step 1: Feed-check result contract, full offline test suite, and checkpoint.
- Step 2: Bounded live feed check and result.
- Steps 3–7: Verify/build commands, admin and serve endpoints, decision log, replay report, final checkpoint.

## Known walls with fallback
- Live Pocket Option and Telegram behavior is UNVERIFIED; use offline mocks where possible.
- Existing notes and replay output mention 10-second candles, while the goal and current configuration specify 15 seconds; verify actual runtime before recording replay claims.
- GitHub push availability is UNVERIFIED; preserve local commits if remote push fails.

## Needs your action
- None identified yet.

## Next step
- Step 1: Fix the feed-check result contract, run the offline suite, and record verified results.

## Last run date
- 2026-10-24; code inspection only, no tests or live connections run.

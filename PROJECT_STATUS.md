# GBot status

## Goal
Read-only Pocket Option signals from 15-second candles, expiring after 35 seconds.
Users place any trades themselves; the bot never submits trades.

## Verified
- Code uses 15-second candles and 35-second signal expiration.
- Replit run command is `uv run --locked python main.py --run serve`.
- `feed-check` result handling and failure exit are covered by offline tests.

## Not done / unverified
- Telegram subscription controls, command scopes, and live test-signal pipeline need fixes and verification.
- Admin metrics and the 35-second live-tick decision outcomes need completion.
- Replay output needs to use the current 15-second / 35-second definitions.
- Prior live feed claim (62 ticks in 30 seconds) is UNVERIFIED until repeated once in this run.
- Telegram live checks, replay table, and remote push are NOT RUN.

## Walls with fallback
- If Postgres is unavailable, subscriber state falls back to `data/subscribers.json`.
- If the live broker or Telegram check cannot complete, report its redacted error and leave it NOT RUN.
- The protected web status endpoint needs `ADMIN_TOKEN`; public `/health` does not.

## Needs your action
- None identified yet. Any unavailable or invalid secret will be named without showing its value.

## Next step
- Finish Steps 1–7 in order, run only the requested live checks, then commit and push.

## Last run date
- 2026-10-24; baseline offline suite: 96 passed, 2 failed (one stale replay assertion, one test fixture affected by an inherited `TELEGRAM_ADMIN_ID`).

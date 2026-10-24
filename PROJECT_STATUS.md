# GBot status

## Goal
- Read-only Pocket Option signals from 15-second candles, with 35-second expiry.
- No trade execution; the user places any trades independently.

## Verified in this run
- Code defaults and unit tests confirm 15-second candles and 35-second expiry.
- Replay and paper outcome defaults now use only the configured 35-second expiry.
- `feed-check` result handling and failure reporting are covered by offline tests.
- Serve test now clears inherited environment variables; focused serve test passes.
- Full offline suite: 104 tests run; 2 fail in Telegram reply expectations scheduled for Step 2.
- Prior live-feed tick-count claim remains UNVERIFIED.
- Live broker, Telegram, 20/40-pair feed, and push checks: NOT RUN yet.

## Walls with fallback
- Subscriber storage uses `data/subscribers.json` if Postgres is unavailable.
- Live broker or Telegram failures must be reported without exposing secret values.
- `/admin/status` needs `ADMIN_TOKEN`; public `/health` does not.

## Needs your approval
- No strategy threshold changes are authorized in this run.

## Needs your action
- If a live check finds an expired or invalid broker session, replace `PO_SSID` in Replit Secrets without sharing its value.

## Next step
- Continue with runtime-data Git hygiene, then Telegram replies and the pair-list/funnel work.

## Last run date
- 2026-10-24.

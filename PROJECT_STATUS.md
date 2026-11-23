# GBot — Phase 3 status (2026-11-23)

## Commits on main (pushed via GitHub API, remote tree verified after each)
| Checkpoint | Commit | Scope |
|---|---|---|
| Start | `7fd3870` | Phase 2 baseline |
| 1 | `336a0fcdf57a2c2b08d3f01c50ef339670e2364e` | 512 MB memory tuning, handshake challenge guidance, Direct SSID removed, expired-session teardown |
| 2 | `5b86d1905ce78598d1ccee1a5cf120f3de16e0d0` | Demo/Real vault isolation, pooled balance reads, Real `isDemo=0` fix |
| 3 | `ed035dd74ccc29ad54ea4aed67a7c4458fb45179` | Fixed / Martingale stake selector, stop-loss exposure |
| 4 | `3b65f1f118333504ce5ca0b100e4ba2688526ed3` | Min-payout filter and multi-asset payout routing |
| 5 | `dd1201d1a7bb4ef14f085302fb8929585e2b46c9` | Telegram opened / settled / daily summary tracking |
| 6 | commit adding this file | Final verification, PostgreSQL Phase 3 test |

## Implemented
- Memory (render.yaml, Dockerfile): MALLOC_ARENA_MAX=2, MALLOC_TRIM_THRESHOLD_=131072, NODE_OPTIONS 256 MB
  heap cap, no bytecode writes; idle pooled sockets closed after 60 s, pool capped at 16.
- Handshake reports Cloudflare Turnstile, CAPTCHA and 2FA with clear next steps instead of timeouts.
- Direct SSID login removed (buttons, menus, `/at_ssid`, `/auth/ssid`); pasted session strings are deleted
  and refused. Sign-in is email/password handshake into the encrypted vault only; passwords never stored.
- Expired sessions close their pooled connection and prompt `/at_login`.
- Demo and Real sessions live in separate vault rows keyed by (user, kind); one never overwrites the other.
- Balance reads reuse worker or pooled connections with a rate-limit cooldown; no short-lived sockets.
- Account kind comes from the session's own `isDemo` flag; valid Real (`isDemo=0`) sessions start cleanly.
- Stake mode `fixed` or `martingale` (multiplier 2.0, max 3 steps, base stake); resets on win or after the
  last step; a draw keeps the step; daily stop-loss checks worst-case open exposure before every order.
- Payout routing: candidates below `min_payout` (default 80) are dropped, the rest ranked by live payout
  (Brent Oil OTC, then Gold OTC on ties). Only setups computed on their own asset are eligible.
- Telegram: Trade Opened (mode, asset, BUY/SELL, stake, payout, 1m), Trade Settled (WIN/LOSS, net P/L,
  balance, win streak / step), Daily Summary (trades, win rate, realized P/L). Alerts are queued per chat,
  sent in order off the trading loop, retried on 429/5xx, capped at 50 queued.
- Unchanged: 1-minute candle price-action engine and candle close timing.

## Offline verification (Checkpoint 6)
- `pytest autotrade/tests/` with a throwaway local PostgreSQL 16: 212 passed, 0 failed, 0 skipped (3 runs).
- Same without a database: 208 passed, 4 PostgreSQL tests skipped.
- `pytest tests/`: 170 passed.
- New tests: Checkpoint 3 (stake ladder), 4 (14 routing), 5 (8 tracking), PostgreSQL settings + dual vault.
- Schema applied twice (idempotent); invalid `stake_mode` rejected by its check constraint.

## Live verification
- Render deploy and memory under real load: NOT RUN.
- Production PostgreSQL migration: NOT RUN.
- Pocket Option login, Turnstile/CAPTCHA/2FA screens, Demo and Real balances: NOT RUN.
- Live payouts, multi-asset routing and order placement: NOT RUN.
- Martingale progression on real settlements: NOT RUN.
- Telegram delivery of trade alerts and daily summary: NOT RUN.
- No secrets were printed or logged; tests use fake brokers and fake sessions.

## Risks / limits
- No payout level is guaranteed; assets without their own valid setup are never traded.
- Martingale raises exposure after losses; the daily stop-loss is the hard limit.

## Needs your action
- Deploy, apply `autotrade/schema.sql`, then confirm a Demo login, balance and one Demo trade with alerts
  before switching to Real.

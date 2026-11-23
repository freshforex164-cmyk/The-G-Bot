# GBot — Phase 3 status (2026-11-23, signal pipeline update)

## Commits on main (pushed via GitHub API, remote tree verified after each)
| Checkpoint | Commit | Scope |
|---|---|---|
| Start | `7fd3870` | Phase 2 baseline |
| 1 | `336a0fcdf57a2c2b08d3f01c50ef339670e2364e` | 512 MB memory tuning, handshake challenge guidance, Direct SSID removed, expired-session teardown |
| 2 | `5b86d1905ce78598d1ccee1a5cf120f3de16e0d0` | Demo/Real vault isolation, pooled balance reads, Real `isDemo=0` fix |
| 3 | `ed035dd74ccc29ad54ea4aed67a7c4458fb45179` | Fixed / Martingale stake selector, stop-loss exposure |
| 4 | `3b65f1f118333504ce5ca0b100e4ba2688526ed3` | Min-payout filter and multi-asset payout routing |
| 5 | `dd1201d1a7bb4ef14f085302fb8929585e2b46c9` | Telegram opened / settled / daily summary tracking |
| 6 | `60c93f447581d5bd87a8e8246139864eb4e6ecfe` | Final verification, PostgreSQL Phase 3 test |
| 1b | `451f531896b0c8b0e985211b177d06e19f42e5c4` | "Sign in again anyway" replaces stale sessions; login view never blank |
| 2b | `90097575c84aee3617df3cf4d8d6428b864b4d86` | PostgreSQL check: wiping one slot keeps the other |
| 6b | `a2f0443821de9e2b2594b79d3bda37259aea545e` | Re-login verification report |
| 7 | commit adding this version | Signal → trade pipeline fix, Auto-Trade vs Signal Mode, threaded alerts |

## Implemented
- Memory (render.yaml, Dockerfile): MALLOC_ARENA_MAX=2, MALLOC_TRIM_THRESHOLD_=131072, NODE_OPTIONS 256 MB
  heap cap, no bytecode writes; idle pooled sockets closed after 60 s, pool capped at 16.
- Handshake reports Cloudflare Turnstile, CAPTCHA and 2FA with clear next steps instead of timeouts.
- Direct SSID login removed (buttons, menus, `/at_ssid`, `/auth/ssid`); pasted session strings are deleted
  and refused. Sign-in is email/password handshake into the encrypted vault only; passwords never stored.
- Expired sessions close their pooled connection and prompt `/at_login`.
- "Sign in again anyway" (force): on success every captured slot is overwritten, uncaptured slots are
  deleted; no stale SSID survives. Reproduced before the fix (old sessions kept), verified after.
- Login view: Telegram script async (was render-blocking: blank page), loading panel until the first
  picture with Try again / Cancel after 30 s, 204 while Chromium starts, 8 s screenshot cap, any phone
  size clamped, proxy HTTPS headers. Closing the page or 150 s without polling frees Chromium.
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
- Signal pipeline (7): GBot hands each confirmed signal to every auto-trader's bot the moment it is
  confirmed, before the Telegram card queue (slow card fan-out used to expire signals), and for every
  scanned asset, not only the chart the user chose. Auto-traders get no signal text; others get cards.
- Bot side: GBot signals skip the anti-retry cooldown; trades past their 1-minute expiry no longer block
  the next minute's signal (stop-loss exposure still counts them); signals arriving while reconnecting are
  queued; pre-order balance read capped at 2.5 s; every trade (Demo and Real) uses a 60 s expiry.
- Routine skips (payout, max open trades, expired) post nothing and are counted in `/at_status`; low
  balance and limits are told once. The settled alert replies to its TRADE OPENED message.
- Unchanged: 1-minute candle price-action engine and candle close timing.

## Offline verification (Checkpoint 6)
- Checkpoint 7: `pytest autotrade/tests/` 223 passed, 4 PostgreSQL tests skipped (no database);
  `pytest tests/` 176 passed. New: pipeline (8), dispatch / mode (6). Earlier with PostgreSQL 16: 219 passed.
- Login page driven in headless Chromium at 384x661 against a simulated broker: re-login replaces both
  sessions; loading panel shown, then the live picture; page close cancels the server browser.
- New tests: re-login override (7), stake ladder, routing (14), tracking (8), PostgreSQL settings + dual vault.
- Schema applied twice (idempotent); invalid `stake_mode` rejected by its check constraint.

## Live verification
- Render deploy and memory under real load: NOT RUN.
- Production PostgreSQL migration: NOT RUN.
- Pocket Option login, "Sign in again anyway" on the real site, Turnstile/CAPTCHA/2FA: NOT RUN.
- Login view on a Render instance inside Telegram: NOT RUN. Demo and Real balances: NOT RUN.
- Live payouts, multi-asset routing and order placement: NOT RUN.
- Martingale progression on real settlements: NOT RUN.
- Telegram delivery of trade alerts and daily summary: NOT RUN.
- Signal → order rate on the live scanner and broker (checkpoint 7): NOT RUN.
- No secrets were printed or logged; tests use fake brokers and fake sessions.

## Risks / limits
- No payout level is guaranteed; assets without their own valid setup are never traded.
- Martingale raises exposure after losses; the daily stop-loss is the hard limit.
- A forced re-login that captures only one account type deletes the other; sign in again to restore it.
- A running bot keeps its current connection and uses the new session on its next reconnect.

## Needs your action
- Deploy, apply `autotrade/schema.sql`, then confirm a Demo login, balance and one Demo trade with alerts
  before switching to Real.

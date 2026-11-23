# GBot — Phase 2 checkpoint (2026-11-23)

## Goal
- Stabilize Demo/Real automatic trading while preserving Phase 1, Render configuration and candle strategy.

## Implemented
- Account slots already use (user_id, kind) database keys and account-bound encryption; no schema change needed.
- Strict isDemo 0/1 parsing; legacy stored sessions now validated against their own slot.
- Broker adapter normalizes numeric and string account flags; Real 0 is not treated as truthy.
- Balance reads reuse running workers; slow reads never spawn a duplicate socket.
- Stopped-account reads use a serialized pool, capped at 16 connections, idle cleanup after 60 seconds.
- Changed account ciphertext closes only that account connection; startup and shutdown close pooled sockets.
- render.yaml, health endpoint, Python dependencies, strategy engine and candle timing unchanged.

## Verified / offline
- Baseline autotrade: 144 run, 141 passed, 3 PostgreSQL tests skipped.
- Phase 2 targeted: 27 passed, including 9 new account/pool/real-mode regressions.
- Complete autotrade suite: 153 run, 150 passed, 3 PostgreSQL tests skipped.
- Browser tests in this suite use simulated broker pages, not production credentials.

## Live checks
- GitHub latest source confirmed at c37fc59 before repairs; API access linked, not automatic Git sync.
- Render deployment, production PostgreSQL, real broker accounts and Telegram: NOT RUN.
- No production poller, order, login or database mutation started by these tests.

## Known issues / next phase
- Phase 3 pending: stake selector/ladder, selected-asset scan routing, progression and session P/L.
- No asset or payout is guaranteed. Never transplant a signal to a different chart without a valid setup there.

## Needs your action
- After deploying, verify Demo connection/balance on Render before confirming Real mode.

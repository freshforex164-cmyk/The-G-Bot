---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

G Bots is being built in stages: live data and signal-only paper tracking are complete; next comes an explicitly authorized single demo-trade test, then demo risk-managed trading, then mode-isolated Turso/SQLite-style persistence, Render free-tier deployment with cron keep-alive, and UI last. Real mode stays read-only and must never place orders. Demo and real peak-balance, trade, and settings state must remain separate. Keep `PROJECT_CONTEXT.md` current at the end of every task.

**Why:** The user stated the staged product goal and safety rules on 2026-10-19, including that no trades may be placed except in the explicit demo-trade test and SSIDs must never be logged.

**How to apply:** Preserve explicit `MODE=real` plus `--confirm-real` gating, block real-mode orders at the broker boundary, and use mode-prefixed keys. Do not begin live trading, persistence, deployment, or UI work before its stated stage and user authorization. Update `PROJECT_CONTEXT.md` after every task.

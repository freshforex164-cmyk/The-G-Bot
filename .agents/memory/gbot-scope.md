---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

G Bots has live data, signal-only paper tracking, and a one-shot demo-only order test command. The command is not to be run without explicit user authorization. Next is demo risk management, then mode-isolated Turso/SQLite-style persistence, Render free-tier deployment with cron keep-alive, and UI last. Real mode stays read-only and must never place orders. Demo and real peak-balance, trade, and settings state must remain separate. Keep `PROJECT_CONTEXT.md` current at the end of every task.

**Why:** The user stated the staged product goal and safety rules on 2026-10-19, then explicitly requested the one-shot demo order test on 2026-10-20. They require that no other trades be placed and SSIDs never be logged.

**How to apply:** Preserve explicit `MODE=real` plus `--confirm-real` gating, block real-mode orders at the broker boundary, and use mode-prefixed keys. Never run the one-shot demo command without explicit authorization. Do not begin automated trading, persistence, deployment, or UI work before its stated stage and user authorization. Update `PROJECT_CONTEXT.md` after every task.

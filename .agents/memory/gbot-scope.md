---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

G Bots engine supports demo mode by default and read-only real mode only when `MODE=real` and `--confirm-real` are both set. Real mode and the signal-only strategy must never place orders. Demo and real future storage keys for peak balance, trades, and settings must remain separate. The CLI has watch and signal-only modes; UI and database remain out of scope unless requested.

**Why:** The user explicitly requires real mode to be read-only, requested a signal-only strategy with paper tracking, asked that future demo/real state never mix, and deferred UI and database work.

**How to apply:** Preserve explicit real-mode confirmation, keep strategy output paper-only, deny real-mode order placement at the broker boundary, and use mode-prefixed state keys. Do not add database, UI, or web server unless the user changes scope.

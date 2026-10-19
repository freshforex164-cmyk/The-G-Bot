---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

G Bots engine supports demo mode by default and read-only real mode only when `MODE=real` and `--confirm-real` are both set. Real mode must never place orders. Demo and real future storage keys for peak balance, trades, and settings must remain separate. The async CLI prints balance/payouts and streams up to three eligible pairs as 10-second candles for two minutes. No UI or database is in scope yet.

**Why:** The user explicitly requires real mode to be read-only and asked that future demo/real state never mix, while deferring storage and UI.

**How to apply:** Preserve explicit real-mode confirmation, deny real-mode order placement at the broker boundary, and use mode-prefixed state keys. Do not add indicators, database, UI, or web server unless the user changes scope.

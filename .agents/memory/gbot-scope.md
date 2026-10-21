---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

GBot is a read-only Pocket Option signal scanner. It uses one `PO_SSID`; users place any trades manually. Trade execution, account-mode selection, balances/risk management, and database persistence are out of scope. Telegram is a replaceable notification channel using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**Why:** The user restated the GBot product boundary on 2026-10-21: the bot never places trades, has no database, and users act on its signals manually.

**How to apply:** Keep market-data access read-only and use a single `PO_SSID`. Do not add trade execution, account-mode flows, balance/risk management, or a database. Never print secret values.

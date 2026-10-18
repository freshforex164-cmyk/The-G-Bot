---
name: G Bots engine scope
description: User-stated initial scope for the Pocket Option project.
---

The user renamed the project to G Bots engine and expanded it to Phase 1: an async CLI that connects with a demo session, prints balance and payouts, and streams the top three eligible pairs as 10-second candles for two minutes. The broker's order method is present but must not be called by the Phase 1 runner. No UI, database, or web server is in scope yet.

**Why:** The user explicitly limited Phase 1 to CLI market data and deferred trading execution, UI, persistence, and web serving.

**How to apply:** Keep Phase 1 CLI-only and read-only at runtime; do not add indicators, database, UI, or web server unless the user changes scope.

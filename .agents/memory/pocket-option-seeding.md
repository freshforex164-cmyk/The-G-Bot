---
name: Pocket Option seed sequencing
description: Shared-client behavior when requesting historical candles for multiple pairs.
---

Seed each pair's historical candles sequentially before opening the live tick streams.

**Why:** Concurrent candle-history calls on the shared Pocket Option client stalled in a live run, while sequential history seeding completed and allowed the multi-pair test-signal run to finish.

**How to apply:** Any workflow that fetches candle history for multiple symbols through one `PocketOptionBroker` should serialize those fetches; start concurrent tick streams only after seeding is complete.

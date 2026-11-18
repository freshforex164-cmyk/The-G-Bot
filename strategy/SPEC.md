# GBot pure price-action strategy specification

## Candle and regime rules

- Inputs are completed one-minute OHLC candles, oldest-to-newest. Forming
  candles and intrabar ticks never create strategy signals.
- A signal has a one-minute expiry.
- Regime classification waits for 30 candles and inspects the latest 20.
- A one-candle-lag confirmed swing sequence with rising highs and lows is an
  uptrend; falling highs and lows are a downtrend.
- If neither trend structure is confirmed, consolidation is valid when the
  rolling high-low range is at least 1.5 times the 14-candle average true range.
- Consolidation zones are the outer 25% of that rolling range. An upper-zone
  touch that closes below its high can SELL once per upper structural level;
  a lower-zone touch that closes above its low can BUY once per lower level.

## Trend state machine

- A newly established structural high H in an uptrend can produce one SELL
  pullback on the following candle if that candle fails to break H, or breaks H
  and closes red.
- The pullback candle starts a tracked low L. A later candle that remains at or
  above L produces one BUY continuation; a lower low updates L and delays it.
- Downtrend behavior mirrors this: pullback BUY from structural low L, then
  continuation SELL once a candle remains at or below the tracked pullback high.
- Trend signals strictly alternate Pullback → Continuation → Pullback.
  Regime changes clear the trend phase and fired-level suppression state.
- Startup replays up to 60 historical closed candles to initialize regime,
  structural levels, and alternation state without sending historical signals.
- All notifications are informational. No trade is submitted.

## Debugging and replay

- `uv run --locked python main.py --run strategy-debug --pair <catalogue-id>`
  prints the latest closed-candle regime and active structural levels for one
  allowlisted catalogue asset. It reads market history only and never prints
  credentials.
- `--run replay` uses one-minute historical candles and the configured
  one-minute expiry. Replay outcomes remain approximate candle-close proxies.

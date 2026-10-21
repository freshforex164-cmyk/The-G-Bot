# GBot pullback-reversal strategy specification

## Candle and index convention

- Strategy inputs are completed 10-second OHLC candles only.
- Storage and indicator functions keep candles oldest-to-newest.
- In this specification, index `0` is the latest completed candle, index `1`
  is the candle before it, and increasing indexes mean older candles.
- Trigger A may read the live price and current forming candle. The forming
  candle is never used to calculate S1–S4.
- Trigger B tests the newest completed candle against a setup evaluated on the
  completed candle immediately before it.

## Indicators

- Bollinger Bands use the last 20 closes: `mid = SMA20(close)`,
  `sd = population standard deviation`, `up = mid + 2*sd`,
  `lo = mid - 2*sd`, and `width = (up-lo)/mid`.
- Stochastic uses 14 candles for raw `%K`:
  `raw = 100*(close-LL14)/(HH14-LL14)`, then `K = SMA3(raw)` and
  `D = SMA3(K)`. A zero-range stochastic window is neutral at 50.
- Moving average is `EMA10(close)`.
- `slope(n) = (MA[0]-MA[n])/MA[0]`. The prior MA move is
  `(MA[3]-MA[6])/MA[3]`.

## Setup conditions

Each condition has a config switch (`S1_ENABLED` through `S4_ENABLED`). A
disabled condition is shown as not applicable and does not block a setup.

### SELL

- **S1 Stochastic:** `max(K[0..STOCHASTIC_LOOKBACK_CANDLES-1]) >= 80` and
  `K[0] < D[0]`.
- **S2 MA turn:** `(MA[3]-MA[6])/MA[3] >= MA_RISE_MIN` and
  `slope(3) <= MA_FLAT_TOL`.
- **S3 Bollinger structure:** `(mid[0]-mid[3])/mid[0] <= MID_FLAT_TOL`,
  `up[0] < up[1]`, and `close[0] < mid[0]`.
- **S4 Range filter:** `width[0] / mean(width[0..49]) <= RANGE_MAX_RATIO`.
- SELL's entry zone is between `mid[0]` and `up[0]`.

### BUY (mirror)

- **S1 Stochastic:** `min(K[0..STOCHASTIC_LOOKBACK_CANDLES-1]) <= 20` and
  `K[0] > D[0]`.
- **S2 MA turn:** `(MA[3]-MA[6])/MA[3] <= -MA_RISE_MIN` and
  `slope(3) >= -MA_FLAT_TOL`.
- **S3 Bollinger structure:** `(mid[0]-mid[3])/mid[0] >= -MID_FLAT_TOL`,
  `lo[0] > lo[1]`, and `close[0] > mid[0]`.
- **S4** is the same width ratio filter as SELL.
- BUY's entry zone is between `lo[0]` and `mid[0]`.

## Trigger variants

`TRIGGER_VARIANT` is `A` or `B`.

- **A — live touch:** SELL triggers when live price is in the SELL zone, at or
  above `mid[0]`, and above the forming candle's open while `close[0] < mid[0]`.
  BUY mirrors this: price is in the BUY zone, at or below `mid[0]`, below the
  forming open, while `close[0] > mid[0]`.
- **B — closed rejection:** SELL triggers when a completed candle has
  `high >= mid` and `close < mid`; BUY mirrors this with `low <= mid` and
  `close > mid`. S1–S4 must have held on the immediately prior completed candle.
- Every emitted item is an informational signal only. No trade is submitted.

## Replay and strength

- `--run replay` / `--run strategy-lab` compares all 24 combinations of A/B,
  S2 on/off, S4 on/off, and stochastic lookbacks 3, 5, and 8 on the same data.
- Replay reads `data/candles.csv` when it exists; otherwise it asks the broker
  for up to 3,000 closed 10-second candles per eligible pair. OHLC trigger paths
  and win rates are approximate.
- The lab reports 30/60/120-second win rates and the 52.1% break-even rate at
  92% payout. Payout is not an input to the strength score.
- The target replay strength distribution is approximately p10=20, p50=50,
  p90=85. Actual percentiles are printed with replay output; references in
  `config.py` should be calibrated against recorded replay data, not guessed.

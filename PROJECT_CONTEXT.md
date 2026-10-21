# GBot project context

## Product definition

GBot is a read-only Pocket Option signal scanner. It connects with one
`PO_SSID`, builds 10-second candles for multiple pairs, evaluates the
pullback-reversal strategy, and sends signals through a pluggable notifier.
Telegram is the current notifier. Users decide what to do with signals
themselves. There is no account access beyond market data and the informational
account-type flag, no submitted actions, no stored account funds, and no
database.

The required secret names are `PO_SSID`, `TELEGRAM_BOT_TOKEN`, and
`TELEGRAM_CHAT_ID`. Secret values must never be printed or logged. A missing
`PO_SSID` stops execution with a clear configuration message. Missing Telegram
secrets disable Telegram and leave console notifications available.

## Current capabilities

- Read-only broker connection with bounded reconnect backoff, payout reads,
  historical candle seeding, and live tick subscriptions.
- 10-second OHLC candle construction and Stochastic, moving average, Bollinger
  Bands, and Keltner Channel indicators.
- Pullback-reversal signals with payout-independent strength, ranked active
  signals, per-pair cooldown, duplicate suppression, and expiration.
- `feed-check` prints every top-pair tick with UTC and local timestamps and
  reports the informational account type.
- `replay` evaluates seeded candle history for every pair meeting
  `MIN_PAYOUT`. Its candle-only trigger and outcomes are approximate.
- Console and Telegram notifiers send through an asynchronous, freshness- and
  rate-limited queue.
- Paper outcomes use a delayed entry, 30/60/120-second expiries, and in-memory
  or CSV storage. Ties count as losses.
- `scanner` prints signals, stream health, and paper summaries. `watch` remains
  a plain live-candle viewer.

## Run commands

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
uv run --locked python main.py --run watch
uv run --locked python main.py --run feed-check
uv run --locked python main.py --run replay
uv run --locked python main.py --run notify-test
uv run --locked python main.py --run scanner --minutes 3
```

`watch` runs for `RUN_DURATION_SECONDS`. `feed-check` samples for 30 seconds.
`replay` uses seeded historical candles and prints per-pair and total
approximate summaries. `notify-test` sends one sample notification. Live
commands need valid configured secrets; offline tests use mocks and do not
connect to Pocket Option or Telegram.

## Settings

| Setting | Default | Purpose |
| --- | ---: | --- |
| `MIN_PAYOUT_OPTIONS` | `(80, 85, 88, 92)` | Supported payout threshold choices |
| `MIN_PAYOUT` | `80` | Minimum payout for eligible pairs |
| `MAX_SCAN_PAIRS` | `20` | Maximum concurrent scanner pairs |
| `PAYOUT_REFRESH_SECONDS` | `60` | Pair eligibility refresh interval |
| `MAX_ACTIVE_SIGNALS` | `5` | Maximum signals shown in the active ranking |
| `RECOMMENDED_EXPIRY_SECONDS` | `60` | Signal's recommended expiry |
| `SIGNAL_VALID_SECONDS` | `15` | Signal and queued-notification lifetime |
| `MIN_NOTIFY_STRENGTH` | `0` | Minimum strength for notification |
| `MAX_NOTIFICATIONS_PER_MINUTE` | `6` | Notification rate limit |
| `HUMAN_DELAY_SECONDS` | `5` | Delay before paper entry price is recorded |
| `PAPER_EXPIRIES_SECONDS` | `(30, 60, 120)` | Paper outcome horizons |
| `SIGNALS_CSV_PATH` | `"signals.csv"` | CSV signal-store path |
| `CANDLE_INTERVAL_SECONDS` | `10` | Candle interval |
| `MAX_CANDLES_PER_PAIR` | `300` | Retained/seeded candles per pair |
| `HISTORY_LOOKBACK_SECONDS` | `3000` | History lookback for 300 ten-second candles |
| `TOP_PAIRS_COUNT` | `3` | Highest-payout pairs for watch and feed-check |
| `RUN_DURATION_SECONDS` | `120` | Default watch duration |
| `USE_MOVING_AVERAGE` | `True` | Enable moving-average confirmation |
| `MOVING_AVERAGE_METHOD` | `"EMA"` | Moving-average method |
| `MOVING_AVERAGE_PERIOD` | `10` | Moving-average period |
| `USE_BOLLINGER_BANDS` | `True` | Enable Bollinger confirmation |
| `BOLLINGER_PERIOD` | `20` | Bollinger period |
| `BOLLINGER_STANDARD_DEVIATIONS` | `2.0` | Bollinger band width |
| `USE_STOCHASTIC` | `True` | Enable Stochastic confirmation |
| `STOCHASTIC_PERIOD` | `14` | Stochastic period |
| `STOCHASTIC_SMOOTH_K` / `STOCHASTIC_SMOOTH_D` | `3` / `3` | Stochastic smoothing |
| `STOCHASTIC_OVERBOUGHT` / `STOCHASTIC_OVERSOLD` | `80.0` / `20.0` | Stochastic thresholds |
| `STOCHASTIC_USE_ALTERNATE_LEVELS` | `False` | Enable alternate thresholds |
| `STOCHASTIC_ALTERNATE_OVERBOUGHT` / `STOCHASTIC_ALTERNATE_OVERSOLD` | `70.0` / `30.0` | Alternate thresholds |
| `USE_KELTNER_CHANNEL` | `False` | Enable Keltner calculation |
| `KELTNER_EMA_PERIOD` | `20` | Keltner EMA period |
| `KELTNER_ATR_PERIOD` | `10` | Keltner ATR period |
| `KELTNER_ATR_MULTIPLIER` | `2.0` | Keltner ATR multiplier |
| `STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES` | `5` | Stochastic setup lookback |
| `MA_PRIOR_RISE_LOOKBACK_CANDLES` | `6` | Prior moving-average trend lookback |
| `MA_TURN_LOOKBACK_CANDLES` | `3` | Moving-average turn lookback |
| `BB_TREND_LOOKBACK_CANDLES` | `3` | Bollinger trend lookback |
| `MA_FLAT_TOLERANCE_PERCENT` | `0.05` | Moving-average flattening tolerance |
| `SIGNAL_COOLDOWN_CANDLES` | `3` | Per-pair signal cooldown |
| `STRENGTH_WEIGHTS` | `{stochastic: 30, moving_average: 25, bollinger: 25, middle_band_touch: 20}` | Confirmation-only strength weights; total 100 |
| `STRENGTH_STOCHASTIC_EXTREMITY_REFERENCE` | `15.0` | Stochastic extremity normalization reference |
| `STRENGTH_STOCHASTIC_TURN_REFERENCE` | `10.0` | Stochastic turn normalization reference |
| `STRENGTH_MA_TURN_REFERENCE_PERCENT` | `0.10` | Moving-average turn normalization reference |
| `STRENGTH_BB_SLOPE_REFERENCE_PERCENT` | `0.10` | Bollinger slope normalization reference |
| `STRENGTH_BB_CURVATURE_REFERENCE_PERCENT` | `0.05` | Bollinger curvature normalization reference |
| `STRENGTH_TOUCH_REFERENCE_RANGE_MULTIPLIER` | `0.50` | Middle-band touch cleanliness reference |
| `RECONNECT_MAX_ATTEMPTS` | `6` | Maximum broker reconnect attempts |
| `RECONNECT_BASE_DELAY_SECONDS` | `1.0` | Initial reconnect backoff |
| `RECONNECT_MAX_DELAY_SECONDS` | `30.0` | Maximum reconnect backoff |
| `RECONNECT_CALL_TIMEOUT_SECONDS` | `15.0` | Broker call timeout |
| `CONNECTION_MONITOR_INTERVAL_SECONDS` | `0.25` | Connection monitor interval |
| `INITIAL_CONNECT_TIMEOUT_SECONDS` | `20.0` | Initial connection timeout |
| `POCKET_OPTION_TIME_OFFSET_SECONDS` | `7200` | Provider timestamp offset applied by the library |

## File map

- `main.py` — CLI commands: watch, feed-check, replay, scanner, notify-test.
- `config.py` — secrets loading and scanner, strategy, notification, and paper
  settings.
- `broker/pocket_option.py` — read-only Pocket Option adapter.
- `data/candles.py` — 10-second candle builder and history seeding.
- `indicators/` — moving average, Bollinger Bands, Stochastic, and Keltner.
- `strategy/` — pullback-reversal strategy, replay, and strength scoring.
- `scanner.py` — multi-pair scan loop, active signals, and stream health.
- `notifications.py` — notifier interface, console/Telegram implementations,
  and asynchronous queue.
- `paper/` — delayed paper outcome tracking and in-memory/CSV stores.
- `tests/` — offline tests with mocked broker and Telegram HTTP.

## Parked ideas

- Signal labels `BUY` and `SELL` are direction-only labels; they never submit
  orders. Account-mode selection, `MODE`, `PO_SSID_DEMO`, and `PO_SSID_REAL` are
  retired. Account balance tracking, stake/profit calculations, peak/drawdown
  tracking, `check_win`, `test-trade`, `whoami`, and `--confirm-real` are not
  part of the scanner.
- Automatic execution and a risk manager are out of scope.
- A PWA with web push may be considered later.
- A database and user accounts are out of scope.

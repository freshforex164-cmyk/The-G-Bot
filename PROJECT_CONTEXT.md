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
  Bands, and optional Keltner Channel indicators. Strategy rules use only
  completed candles; index 0 means the latest completed candle.
- Configurable, explainable S1-S4 setup rules, live-touch (A) and closed-rejection
  (B) triggers, with a range-consolidation filter.
- Pullback-reversal signals with payout-independent strength, ranked active
  signals, per-pair cooldown, duplicate suppression, and expiration.
- `feed-check` prints every top-pair tick with UTC and local timestamps and
  reports the informational account type.
- `explain` prints both directional conditions and the current trigger state
  every ten seconds.
- `replay` / `strategy-lab` compares 24 rule variants on the same data and
  reports approximate 30/60/120-second outcomes and 92%-payout break-even.
- `scanner --record` appends every completed scanned candle to
  `data/candles.csv`; replay reads this file when available.
- Console and Telegram notifiers send through an asynchronous, freshness- and
  rate-limited queue.
- `telegram-setup` lists chats returned by Telegram `getUpdates`. It never
  prints the bot token. `test-signals` sends explicitly labeled test messages;
  these are not confirmed signals and never submit trades.
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
uv run --locked python main.py --run telegram-setup
uv run --locked python main.py --run explain --seconds 30
uv run --locked python main.py --run replay
uv run --locked python main.py --run notify-test
uv run --locked python main.py --run test-signals
uv run --locked python main.py --run scanner --minutes 3 --record
```

`watch` runs for `RUN_DURATION_SECONDS`. `feed-check` samples for 30 seconds.
`explain` defaults to the highest-payout three pairs and prints every ten
seconds; `--pair X` selects one. Replay reads recorded candles first; if the
file is absent it asks the broker for up to 3,000 closed 10-second candles per
eligible pair and reports how many were returned. `notify-test` sends one
sample Telegram message. `test-signals` sends up to three marked diagnostic
messages using a fresh live price. Live commands need their configured secrets.
Offline tests use mocks and do not connect to Pocket Option or Telegram.

## Settings

| Setting | Default | Purpose |
| --- | ---: | --- |
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
| `REPLAY_MAX_CANDLES_PER_PAIR` | `3000` | Maximum requested replay candles |
| `DATA_CANDLES_CSV_PATH` | `"data/candles.csv"` | Recorded candle and replay file |
| `TOP_PAIRS_COUNT` | `3` | Default pairs for watch, feed-check, and explain |
| `RUN_DURATION_SECONDS` | `120` | Default watch duration |
| `TRIGGER_VARIANT` | `"A"` | Live touch (`A`) or closed rejection (`B`) |
| `S1_ENABLED` … `S4_ENABLED` | `True` | Individual setup-condition switches |
| `STOCHASTIC_LOOKBACK_CANDLES` | `5` | Stochastic setup lookback |
| `STOCHASTIC_LOOKBACK_OPTIONS` | `(3, 5, 8)` | Strategy-lab variants |
| `STOCHASTIC_PERIOD` | `14` | Raw stochastic lookback |
| `STOCHASTIC_SMOOTH_K` / `STOCHASTIC_SMOOTH_D` | `3` / `3` | Stochastic smoothing |
| `STOCHASTIC_OVERBOUGHT` / `STOCHASTIC_OVERSOLD` | `80` / `20` | Direction thresholds |
| `MOVING_AVERAGE_PERIOD` | `10` | EMA period |
| `MA_RISE_MIN` | `0.0005` | Minimum preceding MA move (ratio) |
| `MA_FLAT_TOL` | `0.0005` | Maximum opposing MA slope (ratio) |
| `MID_FLAT_TOL` | `0.0005` | Bollinger middle-band slope tolerance |
| `BOLLINGER_PERIOD` | `20` | Bollinger period |
| `BOLLINGER_STANDARD_DEVIATIONS` | `2.0` | Bollinger band width |
| `BB_WIDTH_MEAN_LOOKBACK_CANDLES` | `50` | Range-filter width average |
| `RANGE_MAX_RATIO` | `1.2` | Maximum current/average width ratio |
| `SIGNAL_COOLDOWN_CANDLES` | `3` | Per-pair signal cooldown |
| `STRENGTH_WEIGHTS` | `{stochastic: 30, moving_average: 25, bollinger: 25, middle_band_touch: 20}` | Confirmation-only strength weights; total 100 |
| `STRENGTH_*_REFERENCE` | in `config.py` | Strength component calibration |
| `STRENGTH_TOUCH_REFERENCE_RANGE_MULTIPLIER` | `0.50` | Middle-band touch cleanliness reference |
| `BREAKEVEN_PAYOUT_PERCENT` | `92` | Break-even reference for strategy lab |
| `RECONNECT_MAX_ATTEMPTS` | `6` | Maximum broker reconnect attempts |
| `RECONNECT_BASE_DELAY_SECONDS` | `1.0` | Initial reconnect backoff |
| `RECONNECT_MAX_DELAY_SECONDS` | `30.0` | Maximum reconnect backoff |
| `RECONNECT_CALL_TIMEOUT_SECONDS` | `15.0` | Broker call timeout |
| `CONNECTION_MONITOR_INTERVAL_SECONDS` | `0.25` | Connection monitor interval |
| `INITIAL_CONNECT_TIMEOUT_SECONDS` | `20.0` | Initial connection timeout |
| `POCKET_OPTION_TIME_OFFSET_SECONDS` | `7200` | Provider timestamp offset applied by the library |

## File map

- `main.py` — CLI commands, Telegram chat setup, explain, and test signals.
- `config.py` — secrets loading and scanner, strategy, notification, and paper
  settings.
- `broker/pocket_option.py` — read-only Pocket Option adapter.
- `data/candles.py` — 10-second candle builder and history seeding.
- `data/candle_store.py` — CSV recording and replay loading.
- `indicators/` — moving average, Bollinger Bands, Stochastic, and Keltner.
- `strategy/SPEC.md` — exact rule, index, and trigger specification.
- `strategy/` — rule evaluation, pullback-reversal strategy, replay lab, and
  strength scoring.
- `scanner.py` — multi-pair scan loop, active signals, and stream health.
- `notifications.py` — notifier interface, console/Telegram implementations,
  and asynchronous queue.
- `paper/` — delayed paper outcome tracking and in-memory/CSV stores.
- `tests/` — offline tests with mocked broker and Telegram HTTP.

## Safety and scope

- Signal labels `BUY` and `SELL` are direction-only labels; they never submit
  orders. Account-mode selection, `MODE`, `PO_SSID_DEMO`, and `PO_SSID_REAL` are
  retired. Account balance tracking, stake/profit calculations, peak/drawdown
  tracking, `check_win`, `test-trade`, `whoami`, and `--confirm-real` are not
  part of the scanner.
- Automatic execution, monetary risk management, and a database are out of
  scope. No signal, diagnostic message, or replay can place a trade.
- A PWA with web push may be considered later.

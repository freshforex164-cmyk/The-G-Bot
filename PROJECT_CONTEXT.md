# GBot project context

## Product definition

GBot is a read-only Pocket Option signal scanner. It connects with one
`PO_SSID`, scans open OTC pairs, builds 15-second candles, evaluates the
pullback-rejection strategy, and sends signals to subscribed Telegram chats.
It never submits trades or reads account funds. Subscriber state uses Neon
Postgres when `DATABASE_URL` is available, with a local JSON fallback.

The secret names are `PO_SSID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`TELEGRAM_ADMIN_ID`, `REFERRAL_LINK`, and `DATABASE_URL`. Secret values must
never be printed or logged. A missing `PO_SSID` or Telegram bot token stops
`serve` with a clear configuration message. A missing database uses JSON.

## Current capabilities

- Read-only broker connection with bounded reconnect backoff, payout reads,
  historical candle seeding, and live tick subscriptions.
- 15-second OHLC candle construction and Stochastic, moving average, Bollinger
  Bands, and optional Keltner Channel indicators. Strategy rules use only
  completed candles; index 0 means the latest completed candle.
- Configurable, explainable S1-S4 setup rules, live-touch (A) and closed-rejection
  (B) triggers, with a range-consolidation filter.
- Pullback-reversal signals with payout-independent strength, ranked active
  signals, per-pair cooldown, duplicate suppression, and expiration.
- `feed-check` prints every top-pair tick with UTC and local timestamps and
  reports the informational account type.
- `explain` prints both directional conditions and the current trigger state
  every 15 seconds.
- `replay` reports approximate outcomes using 15-second candles and the
  configured 35-second signal expiry; replay fills are estimates.
- `scanner --record` appends every completed scanned candle to
  `data/candles.csv`; replay reads this file when available.
- Console and Telegram notifiers send through an asynchronous, freshness- and
  rate-limited queue.
- `serve` runs the scanner with a Telegram long-poll command service. Public
  commands manage per-chat subscriptions; admin commands are restricted to
  `TELEGRAM_ADMIN_ID`. Subscriber signal fan-out handles 403/429 responses.
- The scanner rejects stale ticks and watches for feed stalls; after repeated
  failed rebuilds it alerts the admin that the SSID may have expired.
- `telegram-setup` lists chats returned by Telegram `getUpdates`. It never
  prints the bot token. `test-signals` sends explicitly labeled test messages;
  these are not confirmed signals and never submit trades.
- Paper outcomes use a delayed entry and in-memory or CSV storage. Ties count
  as losses. Decision-log outcomes use live fire-time entry and a 35-second
  expiry.
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
uv run --locked python main.py --run serve
```

`watch` runs for `RUN_DURATION_SECONDS`. `feed-check` samples for 30 seconds.
`explain` defaults to the highest-payout three pairs and prints every 15
seconds; `--pair X` selects one. Replay reads recorded candles first; if the
file is absent it asks the broker for up to 3,000 closed 15-second candles per
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
| `EXPIRATION_SECONDS` | `35` | Signal expiration |
| `SIGNAL_VALID_SECONDS` | `15` | Signal and queued-notification lifetime |
| `MIN_NOTIFY_STRENGTH` | `0` | Minimum strength for notification |
| `MAX_NOTIFICATIONS_PER_MINUTE` | `6` | Notification rate limit |
| `HUMAN_DELAY_SECONDS` | `5` | Delay before paper entry price is recorded |
| `PAPER_EXPIRIES_SECONDS` | `(30, 60, 120)` | Paper outcome horizons |
| `SIGNALS_CSV_PATH` | `"signals.csv"` | CSV signal-store path |
| `CANDLE_INTERVAL_SECONDS` | `15` | Candle interval |
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
| `MOVING_AVERAGE_PERIOD` | `10` | Moving-average period |
| `MA_RISE_MIN` | `0.0005` | Minimum preceding MA move (ratio) |
| `MA_FLAT_TOL` | `0.0005` | Maximum opposing MA slope (ratio) |
| `MID_FLAT_TOL` | `0.0005` | Bollinger middle-band slope tolerance |
| `BOLLINGER_PERIOD` | `5` | Bollinger period |
| `BOLLINGER_STANDARD_DEVIATIONS` | `1.0` | Bollinger band width |
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
- `data/candles.py` — 15-second candle builder and history seeding.
- `data/candle_store.py` — CSV recording and replay loading.
- `indicators/` — moving average, Bollinger Bands, Stochastic, and Keltner.
- `strategy/SPEC.md` — exact rule, index, and trigger specification.
- `strategy/` — rule evaluation, pullback-reversal strategy, replay lab, and
  strength scoring.
- `scanner.py` — multi-pair scan loop, active signals, and stream health.
- `notifications.py` — notifier interface, console/Telegram implementations,
  and asynchronous queue.
- `telegram_bot.py` — long polling, command routing, admin controls, and
  subscriber signal fan-out.
- `paper/` — delayed paper outcome tracking and in-memory/CSV stores.
- `tests/` — offline tests with mocked broker and Telegram HTTP.

## Safety and scope

- Signal labels `BUY` and `SELL` are direction-only labels; they never submit
  orders. Account-mode selection, `MODE`, `PO_SSID_DEMO`, and `PO_SSID_REAL` are
  retired. Account balance tracking, stake/profit calculations, peak/drawdown
  tracking, `check_win`, `test-trade`, `whoami`, and `--confirm-real` are not
  part of the scanner.
- Automatic execution and monetary risk management are out of scope. No signal,
  diagnostic message, or replay can place a trade.
- A dark-glassmorphism admin PWA is planned with Dashboard, Signals, and
  Settings sections. End users use Telegram, not the PWA.

## Handoff verification

- Verified in code: 15-second candle construction, 35-second expiration, and
  `--run serve` as the Replit run command.
- Verified in code and offline tests: `feed-check` returns structured results
  and exits non-zero when its feed fails.
- UNVERIFIED: the previous live run's reported 62 ticks; repeat once during
  this continuation.
- UNVERIFIED: live Telegram behavior and replay results until their requested
  checks run.

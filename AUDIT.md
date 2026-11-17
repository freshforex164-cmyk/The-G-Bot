# GBot audit

## Modules
- KEEP: `broker/pocket_option.py` (SSID, connection, ticks, payouts, asset metadata, candle history).
- KEEP: `data/candles.py` (tick-to-candle builder); `admin_http.py` (/health, /admin/status, PORT listener).
- KEEP: `telegram_bot.py` polling, command routing, subscriber fan-out; preserve its access controls.
- CHANGE: `main.py`, `scanner.py`, `market_assets.py`, `notifications.py` to the selected-asset price-action flow.
- CHANGE: `config.py` to 1-minute configuration; `strategy/` to configurable price action; `paper/` outcome tracking to signals.csv.
- SILENCE: `subscriber_store.py`; use Postgres only when DATABASE_URL exists, otherwise local JSON.
- KEEP: `data/__init__.py`, `strategy/__init__.py`, `paper/__init__.py`, `indicators/__init__.py` only as needed by retained modules.
- REMOVE: `data/decision_store.py`; `data/candle_store.py`; `paper/store.py`, `paper/tracker.py`.
- REMOVE: `indicators/stochastic.py`, `bollinger_bands.py`, `moving_average.py`, `keltner_channel.py`; retain ATR for RANGE.
- REMOVE: `strategy/rules.py`, `strength.py`, `close_engine.py`, `pullback_reversal.py` and their old variant replay implementation.
- KEEP: test modules covering broker, Telegram transport, health, and subscriber storage; CHANGE strategy, asset, signal, CLI, and scanner tests.
- CHANGE: `pyproject.toml`/`uv.lock` only if the retained Python runtime requires dependency changes; keep the existing project structure.
- KEEP: `artifacts/`, `lib/`, `scripts/`, and pnpm workspace files; they are separate imported workspace services/libraries.

## Settings
- KEEP environment: `PO_SSID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`/`TELEGRAM_USER_ID`, `TELEGRAM_ADMIN_CHAT_ID`/`TELEGRAM_CHAT_ID`, `ADMIN_TOKEN`, `PORT`, `TIMEZONE`, `REFERRAL_LINK`.
- SILENCE: `DATABASE_URL`; optional Postgres only, with JSON-file fallback.
- KEEP connection: `PAYOUTS_TIMEOUT_SECONDS`, `RECONNECT_MAX_ATTEMPTS`, `RECONNECT_BASE_DELAY_SECONDS`, `RECONNECT_MAX_DELAY_SECONDS`, `RECONNECT_CALL_TIMEOUT_SECONDS`, `CONNECTION_MONITOR_INTERVAL_SECONDS`, `INITIAL_CONNECT_TIMEOUT_SECONDS`.
- KEEP feed health: `WATCHDOG_TICK_MAX_AGE_SECONDS`, `WATCHDOG_GLOBAL_STALL_SECONDS`, `WATCHDOG_CALL_TIMEOUT_SECONDS`, `WATCHDOG_CHECK_INTERVAL_SECONDS`, `WATCHDOG_RETRY_SECONDS`, `WATCHDOG_REBUILD_FAILURE_LIMIT`, `POCKET_OPTION_TIME_OFFSET_SECONDS`.
- CHANGE: `CANDLE_SECONDS`, `CANDLE_INTERVAL_SECONDS`, `MAX_CANDLES_PER_PAIR`, `HISTORY_LOOKBACK_SECONDS`, `EXPIRATION_SECONDS`, `RECOMMENDED_EXPIRY_SECONDS`, `SIGNAL_VALID_SECONDS` become 60-second candles/expiry and strategy.json-backed settings.
- REMOVE: `MIN_PAYOUT_OPTIONS`, `MIN_PAYOUT`, `MAX_SCAN_PAIRS`, `MAX_ACTIVE_SIGNALS`, `MIN_NOTIFY_STRENGTH`, `HUMAN_DELAY_SECONDS`, `PAPER_EXPIRIES_SECONDS`, `BREAKEVEN_PAYOUT_PERCENT` as old filter/scanner/paper configuration.
- REMOVE: `MOVING_AVERAGE_METHOD`, `MOVING_AVERAGE_PERIOD`, `BOLLINGER_PERIOD`, `BOLLINGER_STANDARD_DEVIATIONS`, `STOCHASTIC_PERIOD`, `STOCHASTIC_SMOOTH_K`, `STOCHASTIC_SMOOTH_D`, `STOCHASTIC_OVERBOUGHT`, `STOCHASTIC_OVERSOLD`, `MIN_STRATEGY_CANDLES`, `USE_KELTNER_CHANNEL`, `KELTNER_EMA_PERIOD`, `KELTNER_ATR_PERIOD`, `KELTNER_ATR_MULTIPLIER`.
- REMOVE: `TRIGGER_VARIANT`, `STOCHASTIC_LOOKBACK_CANDLES`, `STOCHASTIC_LOOKBACK_OPTIONS`, `MA_RISE_MIN`, `MA_FLAT_TOL`, `MID_FLAT_TOL`, `BB_WIDTH_MEAN_LOOKBACK_CANDLES`, `RANGE_MAX_RATIO`, `S1_ENABLED`, `S2_ENABLED`, `S3_ENABLED`, `S4_ENABLED`, `SIGNAL_COOLDOWN_CANDLES`.
- REMOVE: `PULLBACK_MIN_DROP`, `PULLBACK_MAX_DROP`, `STRENGTH_TARGET_DROP`, `STRENGTH_DROP_RADIUS`, `SELL_STOCHASTIC_FLOOR`, `BUY_STOCHASTIC_CEILING`, `SETUP_WINDOW_CANDLES`, `MAX_BODY_ATR_RATIO`, `MIN_REJECTION_WICK_RATIO`, `STRENGTH_WICK_FULL_SCORE_RATIO`, `EPS_ATR_RATIO`, `MAX_TOUCH_ATR`, `MIDDLE_ENABLED`, `LOWER_ENABLED`, all `STRENGTH_*`, `DATA_CANDLES_CSV_PATH`.
- CHANGE: `ATR_PERIOD` into `strategy.json`; remove obsolete scan/replay defaults `REPLAY_MAX_CANDLES_PER_PAIR`, `REPLAY_HISTORY_LOOKBACK_SECONDS`, `TOP_PAIRS_COUNT`, `RUN_DURATION_SECONDS`.
- KEEP: `MAX_NOTIFICATIONS_PER_MINUTE` (with a 10-second send-age limit); preserve broker reconnect, Telegram API, and HTTP port validation.
- REMOVE `data/decisions.csv`; CHANGE `data/signals.csv` to append-only signal outcomes.

## Commands
- KEEP: `--run serve`, `--run watch`, `--run feed-check`, `--run notify-test`, `--run telegram-setup`, `/health`, `/stop`, `/admin`, `/pause`, `/resume`, `/subscribers`.
- CHANGE: `/start`, `/status`, `/help`, `/register`, `/testsignal`, `/pricecheck`; add `--run pricecheck`; replace `--run replay` with approximate, no-tuning replay.
- CHANGE: `--run scanner`, `--run explain`, and `--run test-signals` only if needed to preserve useful single-asset inspection/test behavior.
- REMOVE: `--run pairs`, `--run strategy-lab`, replay parameter sweeps, `--record`, decision CSV output, and the old multi-pair auto-selection.
- KEEP: options `--minutes`, `--pair`, `--seconds` only for commands that remain; remove the obsolete `--record` option.

## Asset display name to live symbol
- Runtime matching: compare normalized display name, broker name, symbol, and explicit aliases; ignore case, slash, spaces, and OTC suffixes.
- Currency (Currencies): AED/CNY, AUD/CAD, AUD/CHF, CAD/CHF, CAD/JPY, CHF/JPY, EUR/NZD, GBP/USD, KES/USD, MAD/USD, NGN/USD, NZD/USD, QAR/CNY, SAR/CNY, USD/BDT, USD/CLP, USD/INR, USD/VND, YER/USD, EUR/USD, OMR/CNY — live mapping pending.
- Crypto: Avalanche OTC, Dogecoin OTC, Solana OTC — live mapping pending.
- Commodities: Brent Oil OTC, WTI Crude Oil OTC, Silver OTC, Gold OTC — live mapping pending.
- Stocks: American Express OTC, Facebook Inc OTC, Johnson & Johnson OTC, McDonald's OTC — live mapping pending.
- Indices: AUS 200 OTC, 100GBP, 100GBP OTC — live mapping pending.
- Any catalogue name not found in live broker metadata must be flagged as unmatched; unavailable assets cannot produce signals.

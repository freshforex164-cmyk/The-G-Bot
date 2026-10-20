# GBot Engine — Project Context

This is the continuity note for future work. Update this file at the end of every
task. Preserve the staged scope and safety rules below.

## Product goal and working rules

Build a bot that reads live Pocket Option prices, forms 10-second candles,
confirms a pullback-reversal strategy, and eventually places authorized demo
trades with enforced risk limits. The user interface comes last.

Safety rules:

- Never print or log either Pocket Option SSID. Never put secret values in source,
  documentation, tests, or commits.
- Real mode is read-only. Reject all real-account order placement, even after
  `--confirm-real`.
- Do not place trades except during a specifically authorized, explicit demo
  trade test. The current automated tests use fake broker clients; they are not
  live trade tests.
- Keep demo and real account state in separate namespaces.
- Do not start a live broker session merely to verify code or run tests.

## Current stage and intended sequence

- **Built:** Phase 1 — Pocket Option connection adapter, account balance and
  payout reads, historical candle seeding, and 10-second OHLC candles.
- **Built:** Phase 2 — indicator calculations, signal-only pullback/reversal
  strategy, paper outcome tracker, CSV signal output, and demo/real mode
  confirmation and account checks.
- **Built:** An explicitly invoked, one-order demo `test-trade` command, capped at
  $1. It has not been run against Pocket Option.
- **Next:** Risk manager and automatic demo trading.
- **Later:** Persistent storage using a Turso/SQLite-style database, not
  Supabase; deployment to Render's free tier with a cron keep-alive; then UI.

The current risk manager, configurable drawdown stops, persistent database,
Render deployment, and GBot UI are **not implemented**. `MIN_PAYOUT` is currently
one fixed Python setting, not a selector for the planned payout choices. The
paper tracker uses a $1 stake, but it is not a live staking/risk system. The
demo test command is the only CLI path that places an order; never run it without
the user's explicit authorization to place that one demo trade.

## Architecture and file map

### Python GBot engine (the trading product)

- `main.py` — CLI entry point and async orchestration. `watch` reads and prints
  closed candles; `signals` evaluates setups, writes `signals.csv`, and records
  paper outcomes. The separate `test-trade` mode sends exactly one capped demo
  order, waits for settlement, and reads the resulting balance; it does not use
  the strategy.
- `config.py` — mode and secret selection, state-key namespaces, indicator,
  strategy, candle, paper, payout, and connection settings. Full setting
  reference is below.
- `broker/pocket_option.py` — async adapter over
  `BinaryOptionsToolsV2`. Validates the connected account mode, reads balance
  and payouts, retrieves historical candles, streams ticks, and reconnects with
  bounded backoff. Its `buy` method rejects real mode, supports a caller-provided
  stake cap, and submits an order once without retrying an ambiguous result.
- `data/candles.py` — converts broker timestamps and aggregates ticks into
  aligned OHLC candles, retains recent closed candles, and seeds history.
- `indicators/moving_average.py` — SMA and EMA series.
- `indicators/bollinger_bands.py` — middle, upper, and lower bands using
  population standard deviation.
- `indicators/stochastic.py` — slow %K and %D.
- `indicators/keltner_channel.py` — Keltner bands and Wilder ATR; available as
  a calculation module but not part of the current pullback strategy.
- `strategy/pullback_reversal.py` — BUY/SELL setup evaluation on closed candles,
  live middle-band crossing trigger, per-pair cooldown, and signal data. It has
  no broker or order interface.
- `paper/tracker.py` — in-memory signal outcome tracking at expiry. It estimates
  outcomes and profit; it never places orders and does not persist across runs.
- `tests/test_phase1.py` — offline tests for candle handling, configuration,
  broker behavior using a fake client, account-mode gates, and CLI safeguards.
- `tests/test_phase2.py` — offline tests for indicators, strategy, CSV output,
  paper tracking, and signal streaming with synthetic ticks.
- `pyproject.toml`, `uv.lock` — Python metadata and pinned Python dependencies.

### Replit/pnpm starter workspace (separate from the GBot engine)

- `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`,
  `tsconfig*.json`, `.npmrc` — TypeScript/pnpm monorepo setup.
- `artifacts/api-server/` — Express starter API. It currently exposes only
  `GET /api/healthz`; it is not connected to the Python bot.
- `artifacts/mockup-sandbox/` — Vite/React component-preview gallery and UI
  component library; it is not the GBot interface.
- `lib/api-spec/` — health-only OpenAPI definition and Orval code generation.
- `lib/api-client-react/`, `lib/api-zod/` — generated TypeScript API client and
  schemas for that starter API.
- `lib/db/` — Drizzle/Postgres starter package with no GBot schema or persistence.
  It is not the planned Turso/SQLite-style bot storage.
- `scripts/` — workspace helper scripts and a typecheck target.
- `.replit` — declares Node.js 24 and Python 3.12 modules.
- `replit.md` — generated starter notes for the pnpm/API workspace; its Postgres
  `DATABASE_URL` note does not describe a current GBot requirement.
- There is no root README. The configured API and mockup workflows are starter
  services, not the Python engine runner.

## Mode selection, confirmation, and account safety

- `MODE` is read from the environment, stripped, and lowercased. It defaults to
  `demo`; only `demo` and `real` are accepted.
- `MODE=demo` loads `PO_SSID_DEMO`. `MODE=real` loads `PO_SSID_REAL`.
- Real mode also requires the explicit CLI flag `--confirm-real`. The flag does
  not change the selected mode; it only satisfies the extra confirmation check.
- The broker verifies that Pocket Option reports the same account type as
  `MODE` after connecting and refuses a mismatch.
- Real mode can read balance, payouts, candles, and ticks, but the broker rejects
  real-mode orders. `watch` and `signals` do not place orders; `test-trade` is the
  only CLI order path and refuses real mode before loading a secret or connecting.
- `PocketOptionBroker.buy()` independently refuses real mode. Order submission
  bypasses the adapter's retrying read helper so a lost confirmation cannot
  cause an automatic second order.
- The broker library connects during client construction. Do not run the CLI as
  a test or type a real-mode command casually; a CLI run is a live connection.
- SSID format/validation errors name the expected secret but do not echo its
  contents. Provider terminal logging is disabled.

## Secrets

The engine uses these two Replit Secrets, with the values kept only in the
Secrets store:

- `PO_SSID_DEMO` — full Pocket Option demo-session SSID.
- `PO_SSID_REAL` — full Pocket Option real-session SSID.

Never read secret values for documentation, paste them into a command or
terminal, write them to a file, or log them. Configure each account's current
full SSID only in its matching Replit Secret. The automated tests use dummy
sentinel strings and fake broker clients.

## Mode-isolated state key names

These names are reserved in `config.py` for future persistence. No storage
backend currently reads or writes them.

| State | Demo key | Real key |
| --- | --- | --- |
| Peak balance | `g_bots:demo:peak_balance` | `g_bots:real:peak_balance` |
| Trades | `g_bots:demo:trades` | `g_bots:real:trades` |
| Settings | `g_bots:demo:settings` | `g_bots:real:settings` |

Keep all future balance, trade, and settings state mode-separated.

## Strategy behavior

For a SELL setup, the current implementation checks closed candles for:

1. Stochastic %K reached the overbought threshold within the configured recent
   window, and current %K is below %D.
2. The moving average was rising over the prior comparison period, then is flat
   or falling within the configured flat tolerance.
3. The Bollinger middle is non-rising across its lookback, the latest upper band
   is below the preceding upper band, and the last close is below the middle.
4. During the forming candle, live price crosses/touches the recalculated middle
   band from below while the forming candle is bullish.

BUY mirrors the direction and conditions. A signal triggers a per-pair cooldown.
The strategy supports independently toggling Stochastic and moving-average
checks. Bollinger Bands must stay enabled because they define the setup and
trigger. Keltner is disabled by default and is not currently used by this
strategy. Thus, the requested ability to turn every indicator on/off is not yet
fully implemented.

## Risk requirements (planned, not implemented)

- After every completed trade, update peak balance.
- Stop trading at `balance <= peak_balance * (1 - max_drawdown)`.
- Offer max drawdown choices of 40%, 50%, and 60%.
- Offer minimum payout choices of 80%, 85%, 88%, and 92%.
- Minimum stake is $1.
- Risk enforcement must run before any automatic demo order. Real mode remains
  read-only regardless of risk settings.

## `config.py` settings reference

These are the current code defaults. Apart from `MODE`, they are Python
constants rather than environment-backed user preferences.

| Setting | Current value | Purpose |
| --- | --- | --- |
| `DEFAULT_MODE` | `"demo"` | Safe default account mode. |
| `MODE` | `MODE` environment variable, default `"demo"` | Selected, normalized account mode. |
| `VALID_MODES` | `("demo", "real")` | Accepted modes. |
| `SSID_ENV_BY_MODE` | demo → `PO_SSID_DEMO`; real → `PO_SSID_REAL` | Secret chosen for the active mode. |
| `STATE_KEYS_BY_MODE` | See state-key table above | Separate reserved state names per account. |
| `MIN_PAYOUT` | `80` | Current payout filter percent; not yet a selectable 80/85/88/92 option. |
| `CANDLE_INTERVAL_SECONDS` | `10` | Candle interval. |
| `MAX_CANDLES_PER_PAIR` | `300` | Retained closed-candle history per pair. |
| `HISTORY_LOOKBACK_SECONDS` | `3000` | Historical seed window (10 × 300 seconds). |
| `TOP_PAIRS_COUNT` | `3` | Pair limit in watch mode. |
| `RUN_DURATION_SECONDS` | `120` | Watch-mode run length. |
| `MAX_PAIRS` | `5` | Pair limit in signal mode. |
| `SIGNAL_RUN_DEFAULT_MINUTES` | `30` | Default signal-mode duration. |
| `USE_MOVING_AVERAGE` | `True` | Enable/disable the MA strategy condition. |
| `MOVING_AVERAGE_METHOD` | `"EMA"` | MA calculation: EMA or SMA. |
| `MOVING_AVERAGE_PERIOD` | `10` | MA period in candles. |
| `USE_BOLLINGER_BANDS` | `True` | BB strategy condition; strategy requires it enabled. |
| `BOLLINGER_PERIOD` | `20` | BB period in candles. |
| `BOLLINGER_STANDARD_DEVIATIONS` | `2.0` | BB width multiplier. |
| `USE_STOCHASTIC` | `True` | Enable/disable the stochastic strategy condition. |
| `STOCHASTIC_PERIOD` | `14` | Stochastic lookback. |
| `STOCHASTIC_SMOOTH_K` | `3` | %K smoothing. |
| `STOCHASTIC_SMOOTH_D` | `3` | %D smoothing. |
| `STOCHASTIC_OVERBOUGHT` | `80.0` | Default overbought threshold. |
| `STOCHASTIC_OVERSOLD` | `20.0` | Default oversold threshold. |
| `STOCHASTIC_USE_ALTERNATE_LEVELS` | `False` | Select alternate stochastic thresholds. |
| `STOCHASTIC_ALTERNATE_OVERBOUGHT` | `70.0` | Alternate overbought threshold. |
| `STOCHASTIC_ALTERNATE_OVERSOLD` | `30.0` | Alternate oversold threshold. |
| `USE_KELTNER_CHANNEL` | `False` | Keltner feature toggle; not used by current strategy. |
| `KELTNER_EMA_PERIOD` | `20` | Keltner middle EMA period. |
| `KELTNER_ATR_PERIOD` | `10` | Keltner ATR period. |
| `KELTNER_ATR_MULTIPLIER` | `2.0` | Keltner band width multiplier. |
| `STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES` | `5` | Recent candles checked for an overbought reading. |
| `MA_PRIOR_RISE_LOOKBACK_CANDLES` | `6` | Earlier MA comparison point for the prior rise. |
| `MA_TURN_LOOKBACK_CANDLES` | `3` | Recent MA comparison point for the turn/flattening. |
| `BB_TREND_LOOKBACK_CANDLES` | `3` | Lookback used for the middle-band direction. |
| `MA_FLAT_TOLERANCE_PERCENT` | `0.05` | Percentage tolerance for a flat MA. |
| `SIGNAL_COOLDOWN_CANDLES` | `3` | Per-pair closed-candle cooldown after a signal. |
| `EXPIRY_SECONDS` | `30` | Paper outcome expiry. |
| `PAPER_STAKE` | `1.0` | Paper tracker stake in dollars; not live risk control. |
| `TEST_TRADE_STAKE_CAP` | `1.0` | Maximum stake sent by the one-shot demo test command. |
| `PAPER_SUMMARY_EVERY_SIGNALS` | `10` | Paper summary frequency. |
| `SIGNALS_CSV_PATH` | `"signals.csv"` | Signal append-output path. |
| `RECONNECT_MAX_ATTEMPTS` | `6` | Maximum reconnect attempts. |
| `RECONNECT_BASE_DELAY_SECONDS` | `1.0` | Initial reconnect backoff. |
| `RECONNECT_MAX_DELAY_SECONDS` | `30.0` | Maximum reconnect backoff. |
| `RECONNECT_CALL_TIMEOUT_SECONDS` | `15.0` | Timeout for broker calls during reconnect. |
| `CONNECTION_MONITOR_INTERVAL_SECONDS` | `0.25` | Connection/stream monitor interval. |
| `INITIAL_CONNECT_TIMEOUT_SECONDS` | `20.0` | Initial connection and asset wait timeout. |
| `POCKET_OPTION_TIME_OFFSET_SECONDS` | `7200` | Broker timestamp offset removed during candle normalization. |

`ConfigurationError` is the configuration validation exception; it is not a
configurable setting.

## Run commands

Install the Python environment from the committed lockfile:

```sh
uv sync --locked
```

The command above installed `binaryoptionstoolsv2==0.2.15` for the current
workspace. A strictly offline sync initially could not install it because the
wheel was not cached.

Run tests without starting the bot or contacting Pocket Option:

```sh
uv run --offline --locked python -m unittest discover -s tests -v
```

The equivalent direct command after syncing is:
`PYTHONDONTWRITEBYTECODE=1 .pythonlibs/bin/python -m unittest discover -s tests -v`.
Current result: **33 tests passed**. Tests use fake clients and synthetic data.

Live CLI commands (documented for future use only; they were not run here):

```sh
MODE=demo uv run python main.py
MODE=demo uv run python main.py --run watch
MODE=demo uv run python main.py --run signals --minutes 30
MODE=real uv run python main.py --confirm-real
```

The last command opens a real-account, read-only broker session; it is not a
test command. Do not run any live command without an explicit reason and the
correct Replit Secret. There is no Python CLI workflow configured by the
starter workspace.

The one-shot order test command is:

```sh
MODE=demo uv run --locked python main.py --run test-trade
```

This command is **live**: it connects to Pocket Option and places one $1 demo
order. It defaults to `call`, chooses the highest-payout pair currently meeting
`MIN_PAYOUT`, and uses the configured 30-second `EXPIRY_SECONDS`. Optional
choices are `--direction put`, `--pair EURUSD_otc`, and `--expiry 45`. An explicit
pair must currently meet the minimum payout. Do not run this command without
explicit authorization for that one demo trade.

The installed, locked `BinaryOptionsToolsV2==0.2.15` API was inspected: `buy` and
`sell` accept `(asset, amount, time, check_win=False)` and return a trade ID plus
confirmation data; `check_win(id, timeout_seconds=...)` waits for the result and
returns the result/profit mapping. This version exposes no supported-expiry list
API. Provider rejection text is preserved (with the SSID redacted), and any
supported-expiry details included in that response are printed. No order retry
is made.

The separate TypeScript starter workspace is installed with
`pnpm install --frozen-lockfile`; its scripts include `pnpm run typecheck`,
`pnpm run build`, and `pnpm --filter @workspace/api-server run dev`. Those
commands do not run the Python bot or its tests. Do not use the starter
PostgreSQL schema push as bot storage.

## Verification performed for this context update

- Python dependencies synced from `uv.lock` with `uv sync --locked`.
- Ran the full Python unittest suite offline; **42 passed**, including mocked
  success, loss, rejection/no-retry, real-mode refusal, stake-cap, and SSID
  redaction coverage.
- Checked CLI help, Python compilation, and `git diff --check`.
- Verified the installed library order and settlement signatures without
  connecting. No Pocket Option session, market-data request, order, or trade was
  initiated; all order tests use fake clients.

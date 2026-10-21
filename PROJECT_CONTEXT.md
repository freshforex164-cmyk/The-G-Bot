# GBot project context

## Current definition

GBot is a read-only signal scanner for Pocket Option market data. It connects
with one `PO_SSID`, builds 10-second candles, evaluates the pullback-reversal
strategy, and reports signal outcomes. Users place any trades themselves. There
is no account balance logic, trading automation, database, or user interface.
Telegram is intended as one replaceable notification channel.

Secret values must never be printed or logged. Required secret names are
`PO_SSID`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. If `PO_SSID` is missing,
the application stops with a direct configuration error. The account-type flag
from the session is informational only.

## Current stage

- Read-only connection, reconnect backoff, payout reads, tick streams, and
  candle history are retained.
- The 10-second candle builder, four indicators, pullback-reversal strategy,
  and in-memory paper outcome tracker are retained.
- The previous account-mode selection and single-order test implementation have
  been removed.
- Work in progress: feed validation, replay, multi-pair scanning, notifications,
  and multi-expiry paper scoring.

## File map

- `main.py` — command-line entry point and live candle viewer.
- `config.py` — the single-session configuration and indicator/strategy values.
- `broker/pocket_option.py` — read-only Pocket Option adapter, bounded
  reconnect, payout reads, candle history, and tick subscription.
- `data/candles.py` — 10-second OHLC candle builder and seed loader.
- `indicators/` — moving average, Bollinger Bands, stochastic, and Keltner
  Channel calculations.
- `strategy/pullback_reversal.py` — BUY/SELL directional signal setup and
  middle-band trigger. Direction words describe signals, not execution.
- `paper/tracker.py` — in-memory, price-only paper outcomes.
- `tests/` — offline mocked broker, candle, indicator, strategy, and tracker
  coverage.

## Parked ideas

- Auto-trading is out of scope; GBot is a scanner and users act on signals.
- A risk manager is out of scope.
- PWA and web push may be considered later.
- A database and user accounts are out of scope.

## Initial command

Install from the Python lockfile and run the candle viewer:

```sh
uv sync --locked
uv run --locked python main.py --run watch
```

The viewer uses the configured read-only session. No account mode is selected.

# GBot

GBot is a read-only Pocket Option signal scanner. It evaluates market data and
reports signals; users make their own decisions. Never add execution, account
funds/risk management, or a database. Never print or log secret values.

## Run

- `uv sync --locked` — install from the Python lockfile.
- `uv run --locked python -m unittest discover -s tests -v` — run offline tests.
- `uv run --locked python main.py --run watch` — view live candles.
- `uv run --locked python main.py --run feed-check` — inspect the live feed.
- `uv run --locked python main.py --run replay` — approximate candle replay.
- `uv run --locked python main.py --run notify-test` — send a sample notification.
- `uv run --locked python main.py --run scanner --minutes 3` — scan for signals.

Live commands require `PO_SSID`. Telegram notifications additionally use
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; without either, scanning continues
with console notifications.

See `PROJECT_CONTEXT.md` for the product definition, settings, architecture,
and parked ideas.

# GBot

GBot is a read-only Pocket Option signal scanner. It evaluates market data and
reports signals; users place any trades themselves. Never add trade execution,
account-funds access, or monetary risk management. Never print or log secret
values, including broker sessions, Telegram tokens, admin tokens, database URLs,
or referral links.

## Run

- `uv sync --locked` — install from the Python lockfile.
- `uv run --locked python -m unittest discover -s tests -v` — run offline tests.
- `uv run --locked python main.py --run serve` — run the scanner, Telegram bot,
  and HTTP health endpoints together.
- `uv run --locked python main.py --run watch` — view live candles.
- `uv run --locked python main.py --run feed-check` — inspect the live feed.
- `uv run --locked python main.py --run replay` — approximate candle replay.
- `uv run --locked python main.py --run notify-test` — send a sample notification.
- `uv run --locked python main.py --run scanner --minutes 3` — scan for signals.

Live scanning requires `PO_SSID`. `serve` additionally requires
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID` (or legacy `TELEGRAM_ADMIN_ID`);
`TELEGRAM_ADMIN_CHAT_ID` is an
optional private/group admin-chat override and otherwise admin commands use the
admin user's private chat. `ADMIN_TOKEN` enables `GET /admin/status`; without it
that protected endpoint returns 503 while public `GET /health` remains
available. Subscriber storage uses Postgres only when `DATABASE_URL` is set and
reachable, and falls back to `data/subscribers.json` on database errors.

See `PROJECT_CONTEXT.md` for the product definition, settings, architecture,
and parked ideas.

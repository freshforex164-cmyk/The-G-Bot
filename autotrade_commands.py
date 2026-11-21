"""Telegram commands that control the separate autotrade service (multi-user auto-trading).

Signals stay read-only. These commands only work in a private chat with the bot and only when
AUTOTRADE_API_URL and AUTOTRADE_JWT_SECRET are set; otherwise they answer "not set up".
New users always start in Demo; Live needs /at_live confirm (and the service must allow Live).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from autotrade_client import AutotradeClient, AutotradeError

logger = logging.getLogger(__name__)

AUTOTRADE_COMMANDS = [
    {"command": "autotrade", "description": "Auto-trading overview (Demo by default)"},
    {"command": "at_login", "description": "Connect your Pocket Option account"},
    {"command": "at_start", "description": "Start your auto-trading bot"},
    {"command": "at_stop", "description": "Stop your auto-trading bot"},
    {"command": "at_stats", "description": "Today's trades and results"},
    {"command": "at_set", "description": "Change stake, expiry, limits, cooldown"},
    {"command": "at_live", "description": "Switch to Live (real money)"},
    {"command": "at_demo", "description": "Switch back to Demo"},
    {"command": "at_logout", "description": "Stop the bot and delete your session"},
]
AUTOTRADE_ADMIN_COMMANDS = [
    {"command": "at_halt", "description": "Stop new auto-trades for everyone"},
    {"command": "at_resume", "description": "Allow auto-trades again"},
]
USER_COMMANDS = {c["command"] for c in AUTOTRADE_COMMANDS}
ADMIN_COMMANDS = {c["command"] for c in AUTOTRADE_ADMIN_COMMANDS}
ALL_COMMANDS = USER_COMMANDS | ADMIN_COMMANDS

# /at_set <name> <value>: friendly name -> (service field, type)
SETTINGS: dict[str, tuple[str, type]] = {
    "stake": ("stake", float),
    "expiry": ("expiry_seconds", int),
    "max_trades": ("max_concurrent", int),
    "stop_loss": ("daily_stop_loss", float),
    "take_profit": ("daily_take_profit", float),
    "cooldown": ("cooldown_seconds", int),
    "min_payout": ("min_payout", int),
    "gap": ("gap_threshold", float),
}
SET_USAGE = (
    "Usage: /at_set <name> <value>\n"
    "  stake 2         amount per trade\n"
    "  expiry 60       fixed 1-minute trade length\n"
    "  max_trades 1    open trades at once (1-5)\n"
    "  stop_loss 10    stop for the day after losing this much\n"
    "  take_profit 20  stop for the day after winning this much\n"
    "  cooldown 60     seconds between trades (5-3600)\n"
    "  min_payout 80   skip trades below this payout % (50-100)\n"
    "  gap 0.0015      price gap that triggers a trade"
)
NOT_SET_UP = "Auto-trading isn't set up on this bot yet."
PRIVATE_ONLY = "For your account's safety, auto-trading commands only work in a private chat with the bot."
LIVE_WARNING = (
    "Live mode trades REAL money on your Pocket Option account. Losses are possible, and automated "
    "trading may break Pocket Option's terms.\n\n"
    "Your daily stop-loss and stake limits still apply. To switch, send:\n/at_live confirm"
)

Send = Callable[..., Awaitable[Any]]


def _money(value: Any) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def error_text(exc: AutotradeError) -> str:
    if exc.code == "reauth_required":
        which = "Real" if exc.kind == "real" else "Demo"
        return f"Your Pocket Option {which} session is missing or has expired. Send /at_login to connect again."
    if exc.code == "live_disabled":
        return "Live trading is switched off on this service. You can keep trading in Demo."
    if exc.code == "invalid_settings":
        return f"That setting wasn't accepted: {exc.message or 'check the value and try again.'}"
    if exc.code == "TradingHalted":
        return "Auto-trading is paused by the operator right now. Try again later."
    if exc.code == "CapacityError":
        return "The auto-trading service is full right now. Try again later."
    if exc.code == "ServiceUnavailable":
        return "Pocket Option couldn't be reached. Your session is kept; try again in a minute."
    if exc.status == 0:
        return "The auto-trading service can't be reached right now. Try again in a minute."
    return "Sorry, that didn't work. Try again in a minute."


def settings_summary(s: dict[str, Any]) -> str:
    return (
        f"Stake {_num(s.get('stake'))} · expiry {_num(s.get('expiry_seconds'))} s · "
        f"max open {_num(s.get('max_concurrent'))} · cooldown {_num(s.get('cooldown_seconds'))} s\n"
        f"Daily stop-loss {_num(s.get('daily_stop_loss'))} · take-profit {_num(s.get('daily_take_profit'))} · "
        f"min payout {_num(s.get('min_payout'))}%\n"
        f"Strategy {s.get('strategy')} on {s.get('symbol_otc')} (gap {_num(s.get('gap_threshold'))})"
    )


def overview_text(stats: dict[str, Any]) -> str:
    bot = stats.get("bot", {})
    sessions = stats.get("sessions", {})
    today = stats.get("today", {})
    mode = str(stats.get("mode", "demo")).upper()
    status = "running" if bot.get("running") else str(bot.get("status", "stopped")).replace("_", " ")
    lines = [
        f"Auto-trading · {mode}",
        f"Bot: {status} (open trades: {bot.get('open_trades', 0)})",
        f"Sessions: Demo {sessions.get('demo', {}).get('status', 'missing')} · "
        f"Real {sessions.get('real', {}).get('status', 'missing')}",
        f"Today: {today.get('trades', 0)} trades, {today.get('wins', 0)} won, {today.get('losses', 0)} lost, "
        f"P/L {_money(today.get('realized_pnl'))}",
    ]
    if bot.get("halted_by_operator"):
        lines.append("New trades are paused by the operator.")
    if bot.get("last_error"):
        lines.append(f"Last problem: {bot['last_error']}")
    if bot.get("last_skip_reason"):
        lines.append(f"Last skipped trade: {bot['last_skip_reason']}")
    if stats.get("settings"):
        lines += ["", settings_summary(stats["settings"])]
    return "\n".join(lines)


def stats_text(stats: dict[str, Any]) -> str:
    lines = [overview_text(stats)]
    total = stats.get("all_time", {})
    rate = total.get("win_rate")
    lines += ["", f"All time: {total.get('trades', 0)} trades, win rate "
                  f"{f'{rate}%' if rate is not None else 'n/a'}, P/L {_money(total.get('net_profit'))}"]
    recent = stats.get("recent_trades") or []
    if recent:
        lines += ["", "Recent trades:"]
        for t in recent[:5]:
            when = str(t.get("opened_at") or "")[11:16]
            result = t.get("outcome") or t.get("status")
            profit = f" {_money(t['profit'])}" if t.get("profit") is not None else ""
            lines.append(f"{when} {str(t.get('mode', '')).upper()} {t.get('asset')} {t.get('direction')} "
                         f"{_num(t.get('stake'))} → {result}{profit}")
    return "\n".join(lines)


class AutotradeCommands:
    def __init__(self, client: AutotradeClient | None, send: Send) -> None:
        self.client = client
        self._send = send

    async def reply(self, chat_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
        await self._send(chat_id, text, reply_markup=markup)

    async def handle(self, command: str, chat_id: int, sender_id: int | None, args: str = "",
                     *, is_admin: bool = False) -> None:
        if command in ADMIN_COMMANDS and not is_admin:
            return                                  # same as other admin commands: silent
        if self.client is None:
            await self.reply(chat_id, NOT_SET_UP)
            return
        if command in USER_COMMANDS and (sender_id is None or chat_id != sender_id):
            await self.reply(chat_id, PRIVATE_ONLY)
            return
        try:
            await self._run(command, chat_id, int(sender_id), args.strip())
        except AutotradeError as exc:
            logger.warning("Autotrade %s failed (%s %s).", command, exc.status, exc.code)
            await self.reply(chat_id, error_text(exc))

    async def _run(self, command: str, chat_id: int, uid: int, args: str) -> None:
        c = self.client
        assert c is not None
        if command == "autotrade":
            stats = await c.stats(uid)
            await self.reply(chat_id, overview_text(stats) + "\n\nCommands: /at_login /at_start /at_stop "
                             "/at_stats /at_set /at_live /at_demo /at_logout")
        elif command == "at_login":
            link = c.login_link(uid)
            # Inside Telegram as a Mini App when the service is on HTTPS; plain link otherwise (local testing).
            button: dict[str, Any] = (
                {"text": "Log in to Pocket Option", "web_app": {"url": link}} if link.startswith("https://")
                else {"text": "Log in to Pocket Option", "url": link})
            await self.reply(chat_id, "Tap the button to log in to Pocket Option. The link works for 15 minutes "
                             "and is only for you, so don't forward it. After logging in, send /at_start.",
                             {"inline_keyboard": [[button]]})
        elif command == "at_start":
            await c.start(uid)
            stats = await c.stats(uid)
            await self.reply(chat_id, f"Your bot is running in {str(stats.get('mode', 'demo')).upper()} mode.\n\n"
                             + settings_summary(stats.get("settings", {})))
        elif command == "at_stop":
            await self.reply(chat_id, "Stopping… open trades are allowed to finish first.")
            await c.stop(uid)
            await self.reply(chat_id, "Your bot is stopped.")
        elif command == "at_stats":
            await self.reply(chat_id, stats_text(await c.stats(uid)))
        elif command == "at_set":
            await self._set(chat_id, uid, args)
        elif command == "at_live":
            if args.lower() != "confirm":
                await self.reply(chat_id, LIVE_WARNING)
                return
            s = await c.update_settings(uid, {"mode": "live", "confirm_live": True})
            await self.reply(chat_id, "Live mode is on: trades now use REAL money.\n\n" + settings_summary(s))
        elif command == "at_demo":
            s = await c.update_settings(uid, {"mode": "demo"})
            await self.reply(chat_id, "Demo mode is on.\n\n" + settings_summary(s))
        elif command == "at_logout":
            await c.revoke(uid)
            await self.reply(chat_id, "Your bot is stopped and your Pocket Option session was deleted. "
                             "Send /at_login to connect again.")
        elif command == "at_halt":
            await c.halt(uid)
            await self.reply(chat_id, "Auto-trading halted: no new trades for anyone until /at_resume.")
        elif command == "at_resume":
            await c.resume(uid)
            await self.reply(chat_id, "Auto-trading resumed.")

    async def _set(self, chat_id: int, uid: int, args: str) -> None:
        parts = args.split()
        if len(parts) != 2:
            current = await self.client.settings(uid)  # type: ignore[union-attr]
            await self.reply(chat_id, settings_summary(current) + "\n\n" + SET_USAGE)
            return
        name, raw = parts[0].lower(), parts[1]
        if name not in SETTINGS:
            await self.reply(chat_id, f"Unknown setting '{name[:20]}'.\n\n" + SET_USAGE)
            return
        field, kind = SETTINGS[name]
        try:
            value = kind(raw)
            if not math.isfinite(value):
                raise ValueError
        except ValueError:
            await self.reply(chat_id, f"'{raw[:20]}' isn't a valid {'whole ' if kind is int else ''}number for {name}.")
            return
        if field == "expiry_seconds" and value != 60:
            await self.reply(chat_id, "Expiry is fixed at 60 seconds (1 minute).")
            return
        s = await self.client.update_settings(uid, {field: value})  # type: ignore[union-attr]
        await self.reply(chat_id, f"Saved: {name} = {_num(value)}.\n\n" + settings_summary(s))

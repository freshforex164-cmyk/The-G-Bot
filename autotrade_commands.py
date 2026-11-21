"""Telegram commands that control the separate autotrade service (multi-user auto-trading).

Signals stay read-only. These commands only work in a private chat with the bot and only when
AUTOTRADE_API_URL and AUTOTRADE_JWT_SECRET are set; otherwise they answer "not set up".
New users always start in Demo; Real (Live) needs /at_live confirm. /at_login reuses a saved session
instead of a new browser sign-in; /at_ssid stores a pasted SSID without any browser.
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
    {"command": "at_ssid", "description": "Direct SSID login (paste your Pocket Option SSID)"},
    {"command": "at_start", "description": "Start your auto-trading bot"},
    {"command": "at_stop", "description": "Stop your auto-trading bot"},
    {"command": "at_stats", "description": "Today's trades and results"},
    {"command": "at_balance", "description": "Your Pocket Option balance (fresh reading)"},
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
    "follow": ("follow_signals", bool),
}
ON_WORDS, OFF_WORDS = {"on", "yes", "true", "1"}, {"off", "no", "false", "0"}
SET_USAGE = (
    "Usage: /at_set <name> <value>\n"
    "  stake 2         amount per trade\n"
    "  expiry 60       fixed 1-minute trade length\n"
    "  max_trades 2    open trades at once (1-5)\n"
    "  stop_loss 10    stop for the day after losing this much\n"
    "  take_profit 20  stop for the day after winning this much\n"
    "  cooldown 5      seconds between trades (5-3600)\n"
    "  min_payout 80   skip trades below this payout % (50-100)\n"
    "  follow on       place the GBot signals you receive (off = bot's own engine)"
)
NOT_SET_UP = "Auto-trading isn't set up on this bot yet."
PRIVATE_ONLY = "For your account's safety, auto-trading commands only work in a private chat with the bot."
LIVE_WARNING = (
    "Live mode trades REAL money on your Pocket Option account. Losses are possible, and automated "
    "trading may break Pocket Option's terms.\n\n"
    "Your daily stop-loss and stake limits still apply. To switch, send:\n/at_live confirm"
)

SSID_MENU_TEXT = (
    "🔑 Direct SSID Login (no browser sign-in)\n"
    "Which Pocket Option account is this SSID for? Demo and Real use separate SSIDs."
)


def ssid_steps(kind: str) -> str:
    which, flag = ("Real", "0") if kind == "real" else ("Demo", "1")
    return (
        f"🔑 {which} account SSID\n"
        f"1. Open Pocket Option in a desktop browser, log in and open your {which} cabinet.\n"
        "2. Developer tools → Network → WS → the frame starting with 42[\"auth\",\n"
        f"3. Check it contains \"isDemo\":{flag}, copy the whole frame and send it here "
        "(with or without /at_ssid in front).\n\n"
        "Your message is deleted right away and the SSID is stored encrypted."
    )


SSID_HELP = SSID_MENU_TEXT       # kept for older imports

# Trade amounts offered in the Auto-Trade setup menu: (button label, stake).
STAKE_OPTIONS: tuple[tuple[str, float], ...] = (
    ("$1.00", 1.0), ("$1.50", 1.5), ("$2.00", 2.0),
    ("$2.50", 2.5), ("$5.00 RISK", 5.0), ("$10.00 RISK", 10.0),
    ("$25.00", 25.0), ("$50.00", 50.0), ("$100.00", 100.0),
)
STAKE_VALUES = frozenset(value for _, value in STAKE_OPTIONS)


def _session_kind(mode: Any) -> str:
    return "real" if str(mode) == "live" else "demo"


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
    if exc.code == "invalid_ssid":
        return "That isn't a valid Pocket Option SSID. It must start with 42[\"auth\",. Send /at_ssid for help."
    if exc.code == "ssid_rejected":
        return "Pocket Option rejected that SSID (expired or logged out). Copy a fresh one, or use /at_login."
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
    source = ("Trades: the GBot signals you receive" if s.get("follow_signals", True)
              else f"Trades: own price-action engine on {s.get('symbol_otc')}")
    return (
        f"Stake {_num(s.get('stake'))} · expiry {_num(s.get('expiry_seconds'))} s · "
        f"max open {_num(s.get('max_concurrent'))} · cooldown {_num(s.get('cooldown_seconds'))} s\n"
        f"Daily stop-loss {_num(s.get('daily_stop_loss'))} · take-profit {_num(s.get('daily_take_profit'))} · "
        f"min payout {_num(s.get('min_payout'))}%\n"
        f"{source}"
    )


def _balance_line(bot: dict[str, Any]) -> str | None:
    value = bot.get("balance")
    if not isinstance(value, (int, float)):
        return None
    account = "Real" if bot.get("balance_account") == "real" else "Demo"
    return f"Balance ({account}): {float(value):,.2f}"


def _when(iso: Any) -> str:
    text = str(iso or "")
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 else ""


def balance_text(data: dict[str, Any]) -> str:
    account = "Real" if data.get("account") == "real" else "Demo"
    value = data.get("balance")
    if not isinstance(value, (int, float)):
        reason = BALANCE_ERRORS.get(str(data.get("error")), "Try again in a minute.")
        return f"💰 Balance ({account}): not available yet.\n{reason}"
    if data.get("fresh"):
        return f"💰 Balance ({account}): {float(value):,.2f}\nUpdated just now."
    reason = BALANCE_ERRORS.get(str(data.get("error")), "")
    when = _when(data.get("at"))
    return (f"💰 Last known balance ({account}): {float(value):,.2f}" + (f"\nFrom {when}." if when else "")
            + (f"\nA fresh reading wasn't possible: {reason}" if reason else ""))


BALANCE_ERRORS = {
    "no_session": "Your Pocket Option account isn't connected. Send /at_login first.",
    "session_expired": "Your Pocket Option session has expired. Send /at_login to connect again.",
    "session_unreadable": "Your saved session can't be read. Send /at_login to connect again.",
    "broker_unreachable": "Pocket Option couldn't be reached. Try again in a minute.",
    "bot_not_connected": "Your bot is reconnecting to Pocket Option. Try again in a minute.",
    "balance_unavailable": "Pocket Option didn't report a balance. Try again in a minute.",
}


def results_line(label: str, counts: dict[str, Any], pnl: Any) -> str:
    return (f"{label}: {counts.get('trades', 0)} trades · {counts.get('wins', 0)} wins · "
            f"{counts.get('losses', 0)} losses · {counts.get('draws', 0)} break-even · P/L {_money(pnl)}")


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
        results_line("Today", today, today.get("realized_pnl")),
    ]
    balance = _balance_line(bot)
    if balance:
        lines.insert(2, balance)
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
    lines += ["", results_line("All time", total, total.get("net_profit")),
              f"Win rate: {f'{rate}%' if rate is not None else 'n/a'}"]
    recent = stats.get("recent_trades") or []
    if recent:
        lines += ["", "Recent trades:"]
        for t in recent[:5]:
            when = str(t.get("opened_at") or "")[11:16]
            result = t.get("outcome") or t.get("status")
            result = {"win": "win", "loss": "loss", "draw": "break-even"}.get(str(result), result)
            profit = f" {_money(t['profit'])}" if t.get("profit") is not None else ""
            lines.append(f"{when} {str(t.get('mode', '')).upper()} {t.get('asset')} {t.get('direction')} "
                         f"{_num(t.get('stake'))} → {result}{profit}")
    return "\n".join(lines)


class AutotradeCommands:
    def __init__(self, client: AutotradeClient | None, send: Send, roster: Any = None) -> None:
        self.client = client
        self._send = send
        self.roster = roster          # AutotradeRoster: who gets trades instead of signal cards
        self._ssid_wanted: dict[int, str] = {}   # Demo/Real picked in the /at_ssid menu (small, per user)

    def _roster_add(self, uid: int) -> None:
        if self.roster is not None:
            self.roster.add(uid)

    def _roster_discard(self, uid: int) -> None:
        if self.roster is not None:
            self.roster.discard(uid)

    def login_markup(self, uid: int) -> dict[str, Any]:
        assert self.client is not None
        link = self.client.login_link(uid)
        button: dict[str, Any] = (
            {"text": "Log in to Pocket Option", "web_app": {"url": link}} if link.startswith("https://")
            else {"text": "Log in to Pocket Option", "url": link})
        return {"inline_keyboard": [[button], [{"text": "🔑 Direct SSID Login", "callback_data": "at:ssid"}]]}

    @staticmethod
    def ssid_menu_markup() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "🧪 Demo Account", "callback_data": "at:ssid:demo"},
                                     {"text": "💵 Real Account", "callback_data": "at:ssid:real"}]]}

    @staticmethod
    def stake_rows(current: Any = None) -> list[list[dict[str, str]]]:
        """Trade-amount picker: three per row, the saved amount ticked, then Stop and Main Menu."""
        try:
            chosen = float(current)
        except (TypeError, ValueError):
            chosen = None
        buttons = [{"text": label + (" ✅" if chosen == value else ""), "callback_data": f"at:stake:{value:g}"}
                   for label, value in STAKE_OPTIONS]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([{"text": "🛑 Stop", "callback_data": "mode:signals"},
                     {"text": "🏠 Main Menu", "callback_data": "menu:main"}])
        return rows

    async def setup_state(self, uid: int) -> dict[str, Any]:
        """Everything the Auto-Trade setup screen needs, read from the saved session vault (no browser).

        connected: the saved session for the current mode (Demo/Real) is active, so no new login is needed.
        """
        assert self.client is not None
        try:
            settings = await self.client.settings(uid)
        except AutotradeError as exc:
            if exc.code != "reauth_required":
                raise
            settings = {}
        mode = str(settings.get("mode", "demo"))
        kind = _session_kind(mode)
        try:
            sessions = await self.client.auth_status(uid)
        except AutotradeError as exc:
            if exc.code != "reauth_required":
                raise
            sessions = {}
        connected = (sessions.get(kind) or {}).get("status") == "active"
        balance = None
        if connected:
            try:   # last known reading: quick, no broker round trip
                data = await self.client.balance(uid, fresh=False)
                if isinstance(data.get("balance"), (int, float)) and _session_kind(
                        "live" if data.get("account") == "real" else "demo") == kind:
                    balance = float(data["balance"])
            except AutotradeError:
                balance = None
        return {"settings": settings, "mode": mode, "kind": kind, "connected": connected,
                "sessions": sessions, "balance": balance}

    async def set_stake(self, uid: int, value: float) -> tuple[dict[str, Any], str]:
        """Save a stake from the picker. Returns (settings, note). The daily stop-loss is raised to the stake
        when needed (a stake above it is never accepted); the user is told when that happens."""
        assert self.client is not None
        if value not in STAKE_VALUES:
            raise AutotradeError(422, "invalid_settings", message="choose one of the listed amounts")
        current = await self.client.settings(uid)
        patch: dict[str, Any] = {"stake": value}
        note = ""
        try:
            stop_loss = float(current.get("daily_stop_loss", 0))
        except (TypeError, ValueError):
            stop_loss = 0.0
        if value > stop_loss:
            patch["daily_stop_loss"] = value
            note = (f"Daily stop-loss raised to ${value:.2f} so this amount is allowed "
                    "(change it with /at_set stop_loss <amount>).")
        return await self.client.update_settings(uid, patch), note

    async def fresh_balance(self, uid: int) -> dict[str, Any]:
        assert self.client is not None
        return await self.client.balance(uid, fresh=True)

    @staticmethod
    def live_markup() -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "✅ Yes, switch to Real", "callback_data": "at:liveconfirm"},
                                     {"text": "Stay in Demo", "callback_data": "at:demo"}]]}

    @staticmethod
    def connected_text(status: dict[str, Any]) -> str | None:
        """Saved sessions that are still active, e.g. "Demo ✅ · Real ✅"; None when there are none."""
        marks = [f"{name} ✅" for kind, name in (("demo", "Demo"), ("real", "Real"))
                 if (status.get(kind) or {}).get("status") == "active"]
        return " · ".join(marks) or None

    async def enable_follow(self, uid: int) -> tuple[bool, str, dict[str, Any] | None]:
        """Menu "Start Auto-Trade": place GBot signals for this user automatically.
        Returns (started, message, optional login button)."""
        if self.client is None:
            return False, NOT_SET_UP, None
        try:
            current = await self.client.settings(uid)
            if not current.get("follow_signals", True):
                await self.client.update_settings(uid, {"follow_signals": True})
            await self.client.start(uid)
            stats = await self.client.stats(uid)
        except AutotradeError as exc:
            logger.warning("Auto-trade start from the menu failed (%s %s).", exc.status, exc.code)
            if exc.code == "reauth_required":
                return False, ("Connect your Pocket Option account first: tap the button below, sign in, "
                               "then tap 🤖 Start Auto-Trade again."), self.login_markup(uid)
            return False, error_text(exc), None
        self._roster_add(uid)
        return True, str(stats.get("mode", "demo")).upper(), None

    async def disable(self, uid: int) -> None:
        """Back to signal cards at once; the bot stops in the background (open trades finish first)."""
        self._roster_discard(uid)
        if self.client is None:
            return
        try:
            await self.client.stop(uid)
        except AutotradeError as exc:
            logger.warning("Auto-trade stop failed (%s %s).", exc.status, exc.code)

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
            await self.reply(chat_id, overview_text(stats) + "\n\nCommands: /at_login /at_ssid /at_start /at_stop "
                             "/at_stats /at_balance /at_set /at_live /at_demo /at_logout")
        elif command == "at_login":
            # Saved session first: a new browser login per /at_login gets the server's IP blocked.
            connected = None
            try:
                connected = self.connected_text(await c.auth_status(uid))
            except Exception:              # status unknown: offer the normal sign-in
                connected = None
            if connected:
                await self.reply(chat_id, f"✅ Already connected ({connected}). Your saved session is reused, "
                                 "so no new sign-in is needed: tap 🤖 Start Auto-Trade or send /at_start.\n\n"
                                 "Only if Pocket Option logged you out, sign in again or paste an SSID:",
                                 self.login_markup(uid))
                return
            # Inside Telegram as a Mini App when the service is on HTTPS; plain link otherwise (local testing).
            await self.reply(chat_id, "Tap the button to log in to Pocket Option. The link works for 15 minutes "
                             "and is only for you, so don't forward it. After logging in, pick an asset with "
                             "/start and tap 🤖 Start Auto-Trade (or send /at_start).",
                             self.login_markup(uid))
        elif command == "at_ssid":
            if not args:
                await self.reply(chat_id, SSID_MENU_TEXT, self.ssid_menu_markup())
                return
            if args.lower() in {"demo", "real"}:
                self._ssid_wanted[uid] = args.lower()
                await self.reply(chat_id, ssid_steps(args.lower()))
                return
            res = await c.submit_ssid(uid, args)
            kind = "real" if res.get("kind") == "real" else "demo"
            which = "Real" if kind == "real" else "Demo"
            wanted = self._ssid_wanted.pop(uid, None)
            mismatch = (f"\nNote: you chose {wanted.capitalize()}, but this SSID is for your {which} account."
                        if wanted and wanted != kind else "")
            if res.get("verified"):
                head = f"✅ Connected to your Pocket Option {which} account. Your session is saved."
            else:
                head = (f"✅ {which} session saved. Pocket Option couldn't be reached to confirm it right now; "
                        "it is checked again before the first trade.")
            await self.reply(chat_id, f"{head}{mismatch}\nNo browser sign-in needed: choose an asset with /start "
                             "and tap 🤖 Start Auto-Trade.")
        elif command == "at_start":
            await c.start(uid)
            stats = await c.stats(uid)
            if stats.get("settings", {}).get("follow_signals", True):
                self._roster_add(uid)
            await self.reply(chat_id, f"Your bot is running in {str(stats.get('mode', 'demo')).upper()} mode.\n\n"
                             + settings_summary(stats.get("settings", {})))
        elif command == "at_stop":
            self._roster_discard(uid)
            await self.reply(chat_id, "Stopping… open trades are allowed to finish first.")
            await c.stop(uid)
            await self.reply(chat_id, "Your bot is stopped.")
        elif command == "at_stats":
            await self.reply(chat_id, stats_text(await c.stats(uid)))
        elif command == "at_balance":
            await self.reply(chat_id, "Checking your balance…")
            await self.reply(chat_id, balance_text(await c.balance(uid, fresh=True)))
        elif command == "at_set":
            await self._set(chat_id, uid, args)
        elif command == "at_live":
            if args.lower() != "confirm":
                await self.reply(chat_id, LIVE_WARNING, self.live_markup())
                return
            s = await c.update_settings(uid, {"mode": "live", "confirm_live": True})
            await self.reply(chat_id, "Live mode is on: trades now use REAL money.\n\n" + settings_summary(s))
        elif command == "at_demo":
            s = await c.update_settings(uid, {"mode": "demo"})
            await self.reply(chat_id, "Demo mode is on.\n\n" + settings_summary(s))
        elif command == "at_logout":
            self._roster_discard(uid)
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
        if kind is bool:
            word = raw.lower()
            if word not in ON_WORDS | OFF_WORDS:
                await self.reply(chat_id, f"Use '/at_set {name} on' or '/at_set {name} off'.")
                return
            s = await self.client.update_settings(uid, {field: word in ON_WORDS})  # type: ignore[union-attr]
            await self.reply(chat_id, f"Saved: {name} = {'on' if word in ON_WORDS else 'off'}.\n\n" + settings_summary(s))
            return
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

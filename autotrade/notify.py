"""Trade execution & settlement alerts for auto-trading users.

When Auto-Trade is on, GBot does not send the raw signal card; this service tells the user what it did
instead: trade opened, trade settled (win / loss / break-even, profit, new balance) or why a signal was
skipped. Alerts go straight to the user's private Telegram chat (service user ``tg:<id>``) with the
Bot API ``sendMessage`` call only, so it never competes with GBot's polling. Best effort: an alert that
cannot be delivered never affects trading."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

log = logging.getLogger("autotrade.notify")

SKIP_REASONS = {
    "trading_halted": "auto-trading is paused by the operator",
    "account_mode_mismatch": "the connected account type does not match Demo/Live",
    "daily_stop_loss": "your daily stop-loss is reached (trading resumes tomorrow, UTC)",
    "daily_take_profit": "your daily take-profit is reached (trading resumes tomorrow, UTC)",
    "stop_loss_exposure": "open trades could exceed your daily stop-loss",
    "max_concurrent": "the maximum number of open trades is reached",
    "cooldown": "the cooldown between trades is still running",
    "stale_prices": "prices were not fresh enough",
    "payout_unavailable": "the payout could not be read",
    "payout_below_minimum": "the payout is below your minimum",
    "signal_expired": "the signal expired before it could be placed",
    "balance_unavailable": "your account balance could not be read, so no money was risked",
}


def direction_line(direction: str) -> str:
    return "🟢 BUY / CALL" if direction == "CALL" else "🔴 SELL / PUT"


def _money(value: float | None) -> str:
    return f"{float(value or 0):.2f}"


def _account(mode: str) -> str:
    return "Real" if mode == "live" else "Demo"


def setup_name(setup: str | None) -> str:
    """Card name of the market setup, e.g. "uptrend_pullback" -> "Uptrend Pullback"."""
    text = str(setup or "").strip()
    if not text or text == "gbot_signal":
        return "Price Action"
    if "_" in text or text.islower():
        text = " ".join(word.capitalize() for word in text.replace("_", " ").split())
    return text[:48] or "Price Action"


def opened_text(*, mode: str, asset: str, direction: str, setup: str, stake: float, payout: int | None,
                expiry_seconds: int) -> str:
    minutes = max(1, expiry_seconds // 60)
    lines = [f"🤖 Auto-Trade opened · {_account(mode).upper()}", direction_line(direction),
             f"Setup: {setup_name(setup)}"]
    lines += [f"Asset: {asset}", f"Expiration: {minutes} minute{'s' if minutes != 1 else ''}",
              f"Stake: {_money(stake)}", f"Payout: {payout}%" if payout is not None else "Payout: unavailable"]
    return "\n".join(lines)


def settled_text(*, mode: str, asset: str, direction: str, stake: float, outcome: str, profit: float | None,
                 balance: float | None, setup: str = "") -> str:
    head = {"win": f"✅ WIN · +{_money(profit)}", "loss": f"❌ LOSS · -{_money(abs(profit or 0))}",
            "draw": "➖ BREAK EVEN · 0.00"}.get(outcome, "❔ Result unavailable")
    lines = [f"{head} · {_account(mode).upper()}",
             f"{asset} · {direction_line(direction)[2:]} · stake {_money(stake)}",
             f"Setup: {setup_name(setup)}"]
    if balance is not None:
        lines.append(f"Balance ({_account(mode)}): {balance:,.2f}")
    return "\n".join(lines)


def skipped_text(*, asset: str, direction: str, reason: str, setup: str = "") -> str:
    why = SKIP_REASONS.get(reason, reason.replace("_", " "))
    return (f"⏭ Auto-Trade skipped a signal: {asset} {direction_line(direction)[2:]}\n"
            f"Setup: {setup_name(setup)}\nReason: {why}.")


def low_balance_text(*, mode: str, asset: str, direction: str, stake: float, balance: float,
                     setup: str = "") -> str:
    account = "Real" if mode == "live" else "Demo"
    return (f"⚠️ Low balance: Auto-Trade skipped {asset} {direction_line(direction)[2:]}.\n"
            f"Setup: {setup_name(setup)}\n"
            f"Your {account} balance is {_money(balance)}, below the {_money(stake)} stake. "
            "Top up your account or lower the stake with /at_set stake <amount>.")


def failed_text(*, asset: str, direction: str, setup: str = "") -> str:
    return (f"⚠️ Auto-Trade could not place {asset} {direction_line(direction)[2:]} ({setup_name(setup)}). "
            "Pocket Option did not accept the order; nothing was traded.")


class Notifier(Protocol):
    def send(self, user_id: str, text: str) -> None: ...


class NullNotifier:
    def send(self, user_id: str, text: str) -> None:
        return None


class TelegramNotifier:
    """Fire-and-forget delivery; ``sent`` keeps the texts in tests."""

    def __init__(self, token: str, *, opener: Callable[..., Any] | None = None, timeout: float = 10.0):
        self._token, self._timeout = token, timeout
        self._open = opener or urllib.request.urlopen
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def chat_id(user_id: str) -> int | None:
        if not user_id.startswith("tg:"):
            return None
        try:
            value = int(user_id[3:])
        except ValueError:
            return None
        return value if value > 0 else None

    def _post(self, chat_id: int, text: str) -> None:
        body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/sendMessage", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            log.info("trade alert not delivered (HTTP %s)", exc.code)
        except Exception as exc:
            log.info("trade alert not delivered (%s)", type(exc).__name__)

    def send(self, user_id: str, text: str) -> None:
        chat_id = self.chat_id(user_id)
        if chat_id is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(asyncio.to_thread(self._post, chat_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


class RecordingNotifier:
    """Test double."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, user_id: str, text: str) -> None:
        self.sent.append((user_id, text))

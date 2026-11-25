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
import time
from collections import OrderedDict, deque
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

log = logging.getLogger("autotrade.notify")

SKIP_REASONS = {
    "trading_halted": "auto-trading is paused by the operator",
    "account_mode_mismatch": "the connected account type does not match Demo/Live",
    "daily_stop_loss": "your daily stop-loss is reached (trading resumes tomorrow, UTC)",
    "daily_take_profit": "your daily take-profit is reached (trading resumes tomorrow, UTC)",
    "stop_balance": "your balance reached your Stop Balance (auto-trading paused)",
    "stop_loss_exposure": "open trades could exceed your daily stop-loss",
    "max_concurrent": "the maximum number of open trades is reached",
    "cooldown": "the cooldown between trades is still running",
    "stale_prices": "prices were not fresh enough",
    "payout_unavailable": "the payout could not be read",
    "payout_below_minimum": "the payout is below your minimum",
    "signal_expired": "the signal expired before it could be placed",
    "balance_unavailable": "your account balance could not be read, so no money was risked",
    "no_verified_setup": "that asset has no valid price-action setup of its own",
    "duplicate_asset": "another setup on the same asset was evaluated at the same time",
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


def _usd(value: float | None) -> str:
    return f"${float(value or 0):,.2f}"


def _signed_usd(value: float | None) -> str:
    v = float(value or 0)
    return f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def _side(direction: str) -> str:
    return "🟢 BUY" if direction == "CALL" else "🔴 SELL"


def opened_text(*, mode: str, asset: str, direction: str, setup: str, stake: float, payout: int | None,
                expiry_seconds: int, step: int | None = None) -> str:
    """Trade opened: mode, asset, direction, stake, payout, expiry (professional log layout)."""
    minutes = max(1, expiry_seconds // 60)
    stake_line = f"Stake: {_usd(stake)}" + (f" (Martingale step {step})" if step else "")
    return "\n".join([
        f"🤖 TRADE OPENED · {_account(mode).upper()}",
        f"Asset: {asset}",
        f"Direction: {_side(direction)}",
        stake_line,
        f"Payout: {payout}%" if payout is not None else "Payout: unavailable",
        f"Expiry: {minutes}m",
        f"Setup: {setup_name(setup)}",
    ])


def settled_text(*, mode: str, asset: str, direction: str, stake: float, outcome: str, profit: float | None,
                 balance: float | None, setup: str = "", win_streak: int | None = None,
                 step: int | None = None, martingale: bool = False) -> str:
    """Trade settled: outcome, net P/L, current balance, win streak / ladder step."""
    head = {"win": "✅ WIN", "loss": "❌ LOSS", "draw": "➖ DRAW"}.get(outcome, "❔ RESULT UNAVAILABLE")
    lines = [f"{head} · {_account(mode).upper()}",
             f"Asset: {asset} · {_side(direction)[2:]} · stake {_usd(stake)}",
             f"Net P/L: {_signed_usd(profit)}" if profit is not None else "Net P/L: unavailable",
             f"Balance: {_usd(balance)}" if balance is not None else "Balance: unavailable"]
    streak = []
    if win_streak is not None:
        streak.append(f"Win streak: {win_streak}")
    if martingale and step is not None:
        streak.append(f"Next step: {step}")
    if streak:
        lines.append(" · ".join(streak))
    lines.append(f"Setup: {setup_name(setup)}")
    return "\n".join(lines)


def daily_summary_text(*, mode: str, day: str, trades: int, wins: int, losses: int, draws: int,
                       win_rate: float | None, pnl: float) -> str:
    """Daily summary: total trades, win rate, realized P/L for one UTC day and account."""
    rate = f"{win_rate:.1f}%" if win_rate is not None else "n/a"
    return "\n".join([
        f"📊 DAILY SUMMARY · {_account(mode).upper()} · {day} (UTC)",
        f"Total trades: {trades}",
        f"Win rate: {rate} ({wins}W / {losses}L / {draws}D)",
        f"Realized P/L: {_signed_usd(pnl)}",
    ])


def skipped_text(*, asset: str, direction: str, reason: str, setup: str = "", payout: int | None = None,
                 routed_to: str | None = None, routed_payout: int | None = None) -> str:
    why = SKIP_REASONS.get(reason, reason.replace("_", " "))
    if reason == "payout_below_minimum" and payout is not None:
        why = f"{why} ({payout}%)"
    text = (f"⏭ Auto-Trade skipped a signal: {asset} {direction_line(direction)[2:]}\n"
            f"Setup: {setup_name(setup)}\nReason: {why}.")
    if routed_to:
        text += f"\n↪ Routed to {routed_to}" + (f" ({routed_payout}%)" if routed_payout is not None else "") + \
                ", which has its own valid setup."
    return text


def low_balance_text(*, mode: str, asset: str, direction: str, stake: float, balance: float,
                     setup: str = "") -> str:
    account = "Real" if mode == "live" else "Demo"
    return (f"⚠️ Low balance: Auto-Trade skipped {asset} {direction_line(direction)[2:]}.\n"
            f"Setup: {setup_name(setup)}\n"
            f"Your {account} balance is {_money(balance)}, below the {_money(stake)} stake. "
            "Top up your account or lower the stake with /at_set stake <amount>.")


def session_expired_text(kind: str) -> str:
    which = "Real" if kind == "real" else "Demo"
    return (f"🔐 Your Pocket Option {which} session has expired, so its connection was closed and no trades "
            "will be placed on it.\nSend /at_login to reconnect.")


def failed_text(*, asset: str, direction: str, setup: str = "") -> str:
    return (f"⚠️ Auto-Trade could not place {asset} {direction_line(direction)[2:]} ({setup_name(setup)}). "
            "Pocket Option did not accept the order; nothing was traded.")


class Notifier(Protocol):
    def send(self, user_id: str, text: str, *, key: str | None = None, reply_to: str | None = None) -> None: ...


class NullNotifier:
    def send(self, user_id: str, text: str, *, key: str | None = None, reply_to: str | None = None) -> None:
        return None


class TelegramNotifier:
    """Non-blocking, ordered delivery. ``send`` only appends to a small per-chat queue (bounded, oldest
    dropped first) and returns at once, so WebSocket and settlement loops never wait on Telegram. One drain
    task per chat posts in order from a worker thread, retries 429/5xx a few times, and exits when the
    queue is empty (no idle tasks or queues are kept)."""

    MAX_QUEUE = 50
    ATTEMPTS = 3
    MAX_THREADS = 200          # remembered TRADE OPENED message ids (bounded)

    def __init__(self, token: str, *, opener: Callable[..., Any] | None = None, timeout: float = 10.0,
                 sleep: Callable[[float], None] | None = None):
        self._token, self._timeout = token, timeout
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._tasks: set[asyncio.Task] = set()
        self._queues: dict[int, deque[tuple[str, str | None, str | None]]] = {}
        self._draining: dict[int, asyncio.Task] = {}
        self._message_ids: OrderedDict[tuple[int, str], int] = OrderedDict()

    @staticmethod
    def chat_id(user_id: str) -> int | None:
        if not user_id.startswith("tg:"):
            return None
        try:
            value = int(user_id[3:])
        except ValueError:
            return None
        return value if value > 0 else None

    def _post(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> int | bool:
        """One delivery with a few retries (worker thread). The message id (or True) when Telegram accepted
        it, False otherwise. A reply never fails because its parent message is gone."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        body = json.dumps(payload).encode()
        for attempt in range(1, self.ATTEMPTS + 1):
            req = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/sendMessage", data=body,
                                         method="POST", headers={"Content-Type": "application/json"})
            try:
                with self._open(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                try:
                    mid = json.loads(raw or b"{}").get("result", {}).get("message_id")
                except Exception:
                    mid = None
                return int(mid) if isinstance(mid, int) and mid > 0 else True
            except urllib.error.HTTPError as exc:
                retry = exc.code == 429 or exc.code >= 500
                wait = float(attempt)
                if exc.code == 429:
                    try:
                        wait = float(json.loads(exc.read() or b"{}").get("parameters", {}).get("retry_after", 1))
                    except Exception:
                        wait = 1.0
                log.info("trade alert not delivered (HTTP %s, attempt %s)", exc.code, attempt)
                if not retry or attempt == self.ATTEMPTS:
                    return False
                self._sleep(min(max(wait, 0.5), 10.0))
            except Exception as exc:
                log.info("trade alert not delivered (%s, attempt %s)", type(exc).__name__, attempt)
                if attempt == self.ATTEMPTS:
                    return False
                self._sleep(float(attempt))
        return False

    def send(self, user_id: str, text: str, *, key: str | None = None, reply_to: str | None = None) -> None:
        chat_id = self.chat_id(user_id)
        if chat_id is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        q = self._queues.setdefault(chat_id, deque())
        if len(q) >= self.MAX_QUEUE:
            q.popleft()
            log.info("trade alert queue full; oldest alert dropped")
        q.append((text, key, reply_to))
        if chat_id not in self._draining:
            task = loop.create_task(self._drain_chat(chat_id))
            self._draining[chat_id] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _drain_chat(self, chat_id: int) -> None:
        try:
            q = self._queues.get(chat_id)
            while q:
                text, key, reply_to = q.popleft()
                parent = self._message_ids.get((chat_id, reply_to)) if reply_to else None
                sent = await asyncio.to_thread(self._post, chat_id, text, parent)
                if key and isinstance(sent, int) and not isinstance(sent, bool):
                    self._message_ids[(chat_id, key)] = sent
                    while len(self._message_ids) > self.MAX_THREADS:
                        self._message_ids.popitem(last=False)
                if reply_to:
                    self._message_ids.pop((chat_id, reply_to), None)     # settled: thread closed
        finally:
            self._draining.pop(chat_id, None)
            if not self._queues.get(chat_id):
                self._queues.pop(chat_id, None)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


class RecordingNotifier:
    """Test double."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.threads: list[tuple[str | None, str | None]] = []     # (key, reply_to) per message

    def send(self, user_id: str, text: str, *, key: str | None = None, reply_to: str | None = None) -> None:
        self.sent.append((user_id, text))
        self.threads.append((key, reply_to))

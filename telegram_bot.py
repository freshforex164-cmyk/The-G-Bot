"""Telegram long-polling commands and subscriber signal delivery."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import config
from notifications import TelegramNotifier, TelegramSendError, format_signal_message
from strategy.pullback_reversal import Signal
from subscriber_store import SubscriberStore

logger = logging.getLogger("gbot.telegram")

PUBLIC_COMMANDS = [
    {"command": "start", "description": "Start receiving signals"},
    {"command": "stop", "description": "Stop receiving signals"},
    {"command": "register", "description": "Pocket Option account information"},
    {"command": "status", "description": "Check your signal status"},
    {"command": "help", "description": "How GBot works"},
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    {"command": "admin", "description": "Check signal stats"},
    {"command": "health", "description": "Check scanner health"},
    {"command": "pause", "description": "Pause signal scanning"},
    {"command": "resume", "description": "Resume signal scanning"},
    {"command": "subscribers", "description": "List active subscriber chats"},
    {"command": "testsignal", "description": "Send a sample signal to admin"},
]
ADMIN_ONLY_COMMANDS = {
    "admin",
    "health",
    "pause",
    "resume",
    "subscribers",
    "testsignal",
}


class TelegramBotClient:
    """Small Bot API client that never includes its token in error messages."""

    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., object] | None = None,
        request_timeout: float = 8.0,
        poll_http_timeout: float = 35.0,
    ) -> None:
        if not token.strip():
            raise ValueError("Telegram bot token is required.")
        self._token = token
        self._opener = opener or urllib.request.urlopen
        self.request_timeout = request_timeout
        self.poll_http_timeout = poll_http_timeout

    def _call_sync(
        self, method: str, payload: Mapping[str, Any], timeout: float
    ) -> Any:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            response = self._opener(request, timeout=timeout)
            with response:  # type: ignore[attr-defined]
                status = int(
                    getattr(response, "status", response.getcode())  # type: ignore[attr-defined]
                )
                body = response.read()  # type: ignore[attr-defined]
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            TelegramNotifier._raise_api_error(exc.code, body)
            raise AssertionError("unreachable")
        except urllib.error.URLError as exc:
            raise TelegramSendError(
                f"Telegram API request failed ({type(exc.reason).__name__})."
            ) from None
        except TelegramSendError:
            raise
        except Exception as exc:
            raise TelegramSendError(
                f"Telegram API request failed ({type(exc).__name__})."
            ) from None

        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramSendError("Telegram returned an unreadable API response.") from None
        if not isinstance(result, dict):
            raise TelegramSendError("Telegram returned an unexpected API response.")
        if not 200 <= status < 300 or not result.get("ok"):
            TelegramNotifier._raise_api_error(status, body)
        return result.get("result")

    async def call(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._call_sync,
            method,
            payload,
            self.request_timeout if timeout is None else timeout,
        )

    async def get_updates(
        self, *, offset: int | None, poll_timeout: int = 25
    ) -> list[Mapping[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call(
            "getUpdates", payload, timeout=self.poll_http_timeout
        )
        if not isinstance(result, list):
            raise TelegramSendError("Telegram returned an unexpected update list.")
        return [item for item in result if isinstance(item, Mapping)]

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("sendMessage", payload)

    async def answer_callback(self, callback_id: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id})

    async def register_commands(self, admin_id: int | str) -> None:
        default_scope = {"type": "default"}
        admin_scope = {"type": "chat", "chat_id": admin_id}
        await self.call(
            "setMyCommands",
            {"commands": PUBLIC_COMMANDS, "scope": default_scope},
        )
        await self.call(
            "setMyCommands",
            {"commands": ADMIN_COMMANDS, "scope": admin_scope},
        )
        for label, scope in (("default", default_scope), ("admin", admin_scope)):
            commands = await self.call("getMyCommands", {"scope": scope})
            names = (
                [
                    str(command["command"])
                    for command in commands
                    if isinstance(command, Mapping)
                    and isinstance(command.get("command"), str)
                ]
                if isinstance(commands, list)
                else []
            )
            print(f"Telegram commands ({label}): {', '.join(names)}", flush=True)


class TelegramCommandHandler:
    """Route public commands to the source chat and restrict admin actions."""

    def __init__(
        self,
        client: TelegramBotClient,
        subscribers: SubscriberStore,
        *,
        admin_id: int | None,
        referral_link: str | None = None,
        scanner: Any = None,
        tracker: Any = None,
        fanout: Any = None,
        decision_store: Any = None,
        scanner_running: Callable[[], bool] = lambda: False,
        admin_chat_id: int | str | None = None,
        started_at: float | None = None,
        test_signal_provider: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.client = client
        self.subscribers = subscribers
        self.admin_id = admin_id
        self.referral_link = self._valid_referral(referral_link)
        self.scanner = scanner
        self.tracker = tracker
        self.fanout = fanout
        self.decision_store = decision_store
        self.scanner_running = scanner_running
        self.admin_chat_id = admin_chat_id
        self.started_at = started_at or time.monotonic()
        self.test_signal_provider = test_signal_provider

    @staticmethod
    def _valid_referral(link: str | None) -> str | None:
        candidate = (link or "").strip()
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            if candidate:
                logger.warning("REFERRAL_LINK is not a valid HTTP(S) URL; omitting it.")
            else:
                logger.warning("REFERRAL_LINK is missing; account links are omitted.")
            return None
        return candidate

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        try:
            await self._handle_update(update)
        except TelegramSendError:
            raise
        except Exception as exc:
            logger.error("Telegram command failed (%s).", type(exc).__name__)
            chat_id = self._update_chat_id(update)
            if chat_id is not None:
                try:
                    await self.client.send_message(
                        chat_id, "Sorry, that request could not be completed. Try again."
                    )
                except TelegramSendError:
                    logger.warning("Could not send a Telegram command error response.")

    @staticmethod
    def _update_chat_id(update: Mapping[str, Any]) -> int | None:
        message = update.get("message")
        if isinstance(message, Mapping):
            chat = message.get("chat")
            if isinstance(chat, Mapping) and chat.get("id") is not None:
                try:
                    return int(chat["id"])
                except (TypeError, ValueError):
                    return None
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            message = callback.get("message")
            if isinstance(message, Mapping):
                chat = message.get("chat")
                if isinstance(chat, Mapping) and chat.get("id") is not None:
                    try:
                        return int(chat["id"])
                    except (TypeError, ValueError):
                        return None
        return None

    async def _handle_update(self, update: Mapping[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            callback_id = callback.get("id")
            data = str(callback.get("data", ""))
            chat_id = self._update_chat_id(update)
            if callback_id is not None:
                await self.client.answer_callback(str(callback_id))
            if chat_id is not None and data in {
                "signals:start",
                "signals:stop",
                "signals:status",
                "signals:register",
            }:
                await self._dispatch(data.removeprefix("signals:"), chat_id)
            return

        message = update.get("message")
        if not isinstance(message, Mapping):
            return
        chat_id = self._update_chat_id(update)
        sender = message.get("from")
        sender_id: int | None = None
        if isinstance(sender, Mapping) and sender.get("id") is not None:
            try:
                sender_id = int(sender["id"])
            except (TypeError, ValueError):
                pass
        text = message.get("text")
        if chat_id is None or not isinstance(text, str) or not text.startswith("/"):
            return
        command = text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        await self._dispatch(command, chat_id, sender_id=sender_id)

    async def _dispatch(
        self, command: str, chat_id: int, *, sender_id: int | None = None
    ) -> None:
        if command in {"start", "stop", "status", "help", "register"}:
            if command == "start":
                changed = await asyncio.to_thread(
                    self.subscribers.set_subscribed, chat_id, True
                )
                await self.client.send_message(
                    chat_id,
                    "Signals are on for this chat"
                    if changed
                    else "Signals are already on",
                    reply_markup=self._keyboard(chat_id),
                )
            elif command == "stop":
                changed = await asyncio.to_thread(
                    self.subscribers.set_subscribed, chat_id, False
                )
                await self.client.send_message(
                    chat_id,
                    "Signals are off"
                    if changed
                    else "Signals are already off",
                    reply_markup=self._keyboard(chat_id),
                )
            elif command == "status":
                await self._status_reply(chat_id)
            elif command == "help":
                await self.client.send_message(
                    chat_id,
                    "How GBot works. /start begins your signals, /stop pauses them. "
                    "Each signal shows the asset (named as in Pocket Option), "
                    "direction, payout, strength and expiration. Open the asset in "
                    "Pocket Option and place the trade quickly. Strength shows how "
                    "strong the setup is; it is not a guarantee. Signals are not "
                    "financial advice. Trading is risky.",
                )
            else:
                await self._register_reply(chat_id)
            return

        if command in ADMIN_ONLY_COMMANDS:
            if (
                self.admin_id is None
                or sender_id != self.admin_id
                or self.admin_chat_id is None
                or str(chat_id) != str(self.admin_chat_id)
            ):
                return
            if command in {"admin", "health"}:
                await self.client.send_message(chat_id, self._admin_report())
            elif command == "pause":
                pause = getattr(self.scanner, "pause", None)
                if callable(pause):
                    pause()
                    text = "Signal scanning is paused."
                else:
                    text = "Signal scanning is unavailable."
                await self.client.send_message(chat_id, text)
            elif command == "resume":
                resume = getattr(self.scanner, "resume", None)
                if callable(resume):
                    resume()
                    text = "Signal scanning has resumed."
                else:
                    text = "Signal scanning is unavailable."
                await self.client.send_message(chat_id, text)
            elif command == "subscribers":
                chat_ids = await asyncio.to_thread(
                    self.subscribers.subscribed_chat_ids
                )
                text = (
                    f"Active subscribers: {len(chat_ids)}\n"
                    + (
                        "Chats: " + ", ".join(str(item) for item in chat_ids)
                        if chat_ids
                        else "No active subscriber chats."
                    )
                )
                if len(text) > 3900:
                    text = text[:3890] + "\n…"
                await self.client.send_message(chat_id, text, parse_mode=None)
            elif command == "testsignal":
                if self.test_signal_provider is None:
                    await self.client.send_message(
                        chat_id, "Live test-signal pipeline is unavailable."
                    )
                    return
                for message in await self.test_signal_provider():
                    await self.client.send_message(
                        self.admin_chat_id, message, parse_mode=None
                    )
            return

        await self.client.send_message(chat_id, "Unknown command. Use /help.")

    def _keyboard(self, chat_id: int) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        subscriber = self.subscribers.subscribers.get(chat_id)
        if subscriber is not None and subscriber.subscribed:
            rows.extend(
                [
                    [{"text": "Stop signals", "callback_data": "signals:stop"}],
                    [{"text": "Status", "callback_data": "signals:status"}],
                ]
            )
        else:
            rows.extend(
                [
                    [{"text": "Start signals", "callback_data": "signals:start"}],
                    [{"text": "Register", "callback_data": "signals:register"}],
                ]
            )
        return {"inline_keyboard": rows}

    async def _register_reply(self, chat_id: int) -> None:
        if self.referral_link:
            text = (
                "Don't have a Pocket Option account yet? Create one here: "
                f"{html.escape(self.referral_link)}. Already have one? Tap Start signals."
            )
        else:
            text = (
                "The Pocket Option account link is not configured. "
                "Already have an account? Tap Start signals."
            )
        await self.client.send_message(
            chat_id,
            text,
            reply_markup=self._keyboard(chat_id),
        )

    async def _status_reply(self, chat_id: int) -> None:
        subscriber = self.subscribers.subscribers.get(chat_id)
        subscribed = bool(subscriber and subscriber.subscribed)
        if bool(getattr(self.scanner, "is_paused", False)):
            scanner_status = "paused"
        else:
            scanner_status = "running" if self.scanner_running() else "stopped"
        age = getattr(self.scanner, "last_tick_age_seconds", None)
        age_text = "n/a" if age is None else f"{age:.1f}s"
        pair_count = len(getattr(self.scanner, "pairs", {}))
        await self.client.send_message(
            chat_id,
            f"Subscribed: {'yes' if subscribed else 'no'}\n"
            f"Scanner: {scanner_status} (last tick age: {age_text})\n"
            f"Minimum payout: {config.MIN_PAYOUT}%\n"
            f"OTC pairs scanned: {pair_count}",
            reply_markup=self._keyboard(chat_id),
        )

    def admin_metrics(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        signals = getattr(self.scanner, "signals", {}) if self.scanner else {}
        signal_times = []
        for signal in signals.values():
            signal_time = getattr(signal, "time", None)
            if not isinstance(signal_time, datetime):
                continue
            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=timezone.utc)
            signal_times.append(signal_time.astimezone(timezone.utc))
        today = sum(signal_time.date() == now.date() for signal_time in signal_times)
        last_signal_time = (
            max(signal_times).isoformat(timespec="seconds")
            if signal_times
            else None
        )
        broker = getattr(self.scanner, "broker", None) if self.scanner else None
        connected_value = getattr(broker, "is_connected", False)
        connected = bool(
            connected_value() if callable(connected_value) else connected_value
        )
        age = getattr(self.scanner, "last_tick_age_seconds", None)
        age_text = "n/a" if age is None else f"{age:.1f}s"
        scan_latency = getattr(self.scanner, "last_signal_latency_ms", None)
        scan_latency_text = "n/a" if scan_latency is None else f"{scan_latency:.1f}ms"
        delivery_latency = getattr(self.fanout, "last_signal_to_telegram_ms", None)
        delivery_latency_text = (
            "n/a" if delivery_latency is None else f"{delivery_latency:.1f}ms"
        )
        pairs = len(getattr(self.scanner, "pairs", {})) if self.scanner else 0
        store = getattr(self.subscribers, "count", None)
        subscriber_count = store() if callable(store) else 0
        funnel_reader = getattr(self.decision_store, "last_hour_funnel", None)
        funnel = funnel_reader() if callable(funnel_reader) else {}
        return {
            "feed_status": "connected" if connected else "disconnected",
            "last_tick_age_seconds": age,
            "pairs_streaming": pairs,
            "subscriber_count": subscriber_count,
            "signals_today": today,
            "last_signal_time": last_signal_time,
            "last_hour_setup_funnel": funnel,
            "tick_to_signal_latency_ms": scan_latency,
            "signal_to_telegram_latency_ms": delivery_latency,
            "uptime_seconds": max(0, int(time.monotonic() - self.started_at)),
        }

    def _admin_report(self) -> str:
        metrics = self.admin_metrics()
        age = metrics["last_tick_age_seconds"]
        age_text = "n/a" if age is None else f"{age:.1f}s"
        scan_latency = metrics["tick_to_signal_latency_ms"]
        scan_latency_text = "n/a" if scan_latency is None else f"{scan_latency:.1f}ms"
        delivery_latency = metrics["signal_to_telegram_latency_ms"]
        delivery_latency_text = (
            "n/a" if delivery_latency is None else f"{delivery_latency:.1f}ms"
        )
        funnel = metrics["last_hour_setup_funnel"]
        cancelled = funnel.get("cancelled_by_reason", {})
        cancelled_text = (
            ", ".join(
                f"{reason}={count}" for reason, count in sorted(cancelled.items())
            )
            if cancelled
            else "none"
        )
        last_signal_time = metrics["last_signal_time"] or "n/a"
        return (
            f"Feed: {metrics['feed_status']}\n"
            f"SSID: {'configured' if os.environ.get('PO_SSID', '').strip() else 'missing'}\n"
            f"Last tick age: {age_text}\n"
            f"Pairs streaming: {metrics['pairs_streaming']}\n"
            f"Subscribers: {metrics['subscriber_count']}\n"
            f"Signals today: {metrics['signals_today']}\n"
            f"Last signal time: {last_signal_time}\n"
            f"Last-hour setups: armed={funnel.get('armed', 0)}, "
            f"fired={funnel.get('fired', 0)}, "
            f"cancelled by reason: {cancelled_text}, "
            f"expired={funnel.get('expired', 0)}\n"
            f"Tick-to-signal latency: {scan_latency_text}\n"
            f"Signal-to-Telegram latency: {delivery_latency_text}\n"
            f"Uptime: {metrics['uptime_seconds']}s"
        )

class SubscriberFanoutNotifier:
    """Deliver each signal to active subscriber chats with global throttling."""

    def __init__(
        self,
        client: TelegramBotClient,
        subscribers: SubscriberStore,
        *,
        max_messages_per_second: int = 25,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if max_messages_per_second <= 0:
            raise ValueError("max_messages_per_second must be positive")
        self.client = client
        self.subscribers = subscribers
        self.interval = 1.0 / max_messages_per_second
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self.last_signal_to_telegram_ms: float | None = None

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last_send + self.interval - now
            if delay > 0:
                await self._sleep(delay)
            self._last_send = time.monotonic()

    @staticmethod
    def _is_stale(signal: Signal) -> bool:
        return signal.valid_until is not None and signal.valid_until <= datetime.now(
            timezone.utc
        )

    async def send(self, signal: Signal) -> str:
        chat_ids = await asyncio.to_thread(self.subscribers.subscribed_chat_ids)
        sent = 0
        text = format_signal_message(signal)
        for chat_id in chat_ids:
            if self._is_stale(signal):
                logger.info("Signal expired during subscriber fan-out.")
                break
            failures = 0
            while True:
                await self._throttle()
                try:
                    await self.client.send_message(chat_id, text)
                    sent += 1
                    if sent == 1:
                        signal_time = signal.time
                        if signal_time.tzinfo is None:
                            signal_time = signal_time.replace(tzinfo=timezone.utc)
                        self.last_signal_to_telegram_ms = max(
                            0.0,
                            (
                                datetime.now(timezone.utc) - signal_time
                            ).total_seconds()
                            * 1000.0,
                        )
                    break
                except TelegramSendError as exc:
                    if exc.status_code == 403:
                        await asyncio.to_thread(
                            self.subscribers.set_subscribed, chat_id, False
                        )
                        logger.info("Removed a subscriber after Telegram denied delivery.")
                        break
                    if exc.retry_after is not None:
                        if self._is_stale(signal):
                            logger.info("Signal expired while waiting for Telegram rate limit.")
                            break
                        await self._sleep(max(0.1, float(exc.retry_after)))
                        failures += 1
                        if failures < 5:
                            continue
                    raise
        return f"sent to {sent} subscriber chats"


class TelegramPollingService:
    """Long-poll updates sequentially, acknowledge by offset, and back off errors."""

    def __init__(
        self,
        client: TelegramBotClient,
        handler: TelegramCommandHandler,
        *,
        poll_timeout: int = 25,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.handler = handler
        self.poll_timeout = poll_timeout
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._sleep = sleep
        self.offset: int | None = None

    async def poll_once(self) -> int:
        updates = await self.client.get_updates(
            offset=self.offset, poll_timeout=self.poll_timeout
        )
        processed = 0
        for update in updates:
            update_id = update.get("update_id")
            try:
                await self.handler.handle_update(update)
            except TelegramSendError as exc:
                logger.warning(
                    "Telegram update handler failed (%s).",
                    exc.status_code or type(exc).__name__,
                )
            if isinstance(update_id, int):
                self.offset = max(self.offset or update_id, update_id + 1)
            processed += 1
        return processed

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = self.min_backoff
        while not stop_event.is_set():
            try:
                await self.poll_once()
                backoff = self.min_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling failed (%s).", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(self.max_backoff, max(self.min_backoff, backoff * 2))

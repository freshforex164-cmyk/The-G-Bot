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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from market_assets import CATALOG, CATALOG_BY_ID, CATEGORIES
from notifications import TelegramNotifier, TelegramSendError, format_signal_message
from strategy.model import Signal
from subscriber_store import SubscriberStore

logger = logging.getLogger("gbot.telegram")

def configured_timezone() -> ZoneInfo:
    name = os.environ.get("TIMEZONE", "Africa/Lagos").strip() or "Africa/Lagos"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("TIMEZONE is invalid; using Africa/Lagos.")
        return ZoneInfo("Africa/Lagos")


PUBLIC_COMMANDS = [
    {"command": "start", "description": "Choose an asset"},
    {"command": "stop", "description": "Stop receiving signals"},
    {"command": "register", "description": "Pocket Option account information"},
    {"command": "status", "description": "Check your signal status"},
    {"command": "help", "description": "How GBot works"},
    {"command": "pricecheck", "description": "Check your selected asset price"},
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
COMMAND_REPLY_COOLDOWN_SECONDS = 20.0


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
        clock: Callable[[], float] = time.monotonic,
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
        self._clock = clock
        self._last_command_reply_at: dict[tuple[str, str], float] = {}
        self._last_reply_text: dict[str, str] = {}

    async def _send_command_reply(
        self,
        chat_id: int | str,
        text: str,
        *,
        command: str,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = "HTML",
        force: bool = False,
    ) -> bool:
        chat_key = str(chat_id)
        command_key = (chat_key, command)
        now = self._clock()
        last_sent = self._last_command_reply_at.get(command_key)
        if not force and last_sent is not None and now - last_sent < COMMAND_REPLY_COOLDOWN_SECONDS:
            return False
        if not force and self._last_reply_text.get(chat_key) == text:
            return False
        await self.client.send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        self._last_command_reply_at[command_key] = now
        self._last_reply_text[chat_key] = text
        return True

    @staticmethod
    def _update_command(update: Mapping[str, Any]) -> str:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            return str(callback.get("data", "command")).removeprefix("signals:")
        message = update.get("message")
        if isinstance(message, Mapping):
            text = message.get("text")
            if isinstance(text, str) and text.startswith("/"):
                return text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        return "command"

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
                    await self._send_command_reply(
                        chat_id,
                        "Sorry, that request could not be completed. Try again.",
                        command=self._update_command(update),
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
            if chat_id is not None and data.startswith("assetcat:"):
                await self._show_category(chat_id, data.split(":", 1)[1])
                return
            if chat_id is not None and data == "assetback":
                await self._show_categories(chat_id)
                return
            if chat_id is not None and data.startswith("asset:"):
                await self._select_asset(chat_id, data.split(":", 1)[1])
                return
            if chat_id is not None and data.startswith("signals:"):
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
        if command in {"start", "stop", "status", "help", "register", "pricecheck"}:
            if command == "start":
                await self._show_categories(chat_id)
            elif command == "stop":
                changed = await asyncio.to_thread(
                    self.subscribers.set_subscribed, chat_id, False
                )
                await self._send_command_reply(
                    chat_id,
                    "Signals are off"
                    if changed
                    else "Signals are already off",
                    command="stop",
                    reply_markup=self._keyboard(chat_id),
                    force=True,
                )
            elif command == "status":
                await self._status_reply(chat_id)
            elif command == "help":
                await self._send_command_reply(
                    chat_id,
                    "GBot reports informational price-action signals from closed "
                    "1-minute candles. Use /start to choose one asset, /status to "
                    "check your selection, /pricecheck for its latest price, and "
                    "/stop to stop receiving signals. GBot never places trades.",
                    command="help",
                    force=True,
                )
            elif command == "pricecheck":
                await self._pricecheck_reply(chat_id)
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
                await self._send_command_reply(
                    chat_id, self._admin_report(), command=command
                )
            elif command == "pause":
                pause = getattr(self.scanner, "pause", None)
                if callable(pause):
                    pause()
                    text = "Signal scanning is paused."
                else:
                    text = "Signal scanning is unavailable."
                await self._send_command_reply(chat_id, text, command="pause")
            elif command == "resume":
                resume = getattr(self.scanner, "resume", None)
                if callable(resume):
                    resume()
                    text = "Signal scanning has resumed."
                else:
                    text = "Signal scanning is unavailable."
                await self._send_command_reply(chat_id, text, command="resume")
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
                await self._send_command_reply(
                    chat_id, text, command="subscribers", parse_mode=None
                )
            elif command == "testsignal":
                if self.test_signal_provider is None:
                    await self._send_command_reply(
                        chat_id,
                        "Live test-signal pipeline is unavailable.",
                        command="testsignal",
                    )
                    return
                messages = await self.test_signal_provider()
                if messages:
                    await self._send_command_reply(
                        self.admin_chat_id,
                        "\n\n".join(messages),
                        command="testsignal",
                        parse_mode=None,
                    )
            return

        await self._send_command_reply(
            chat_id, "Unknown command. Use /help.", command=command
        )

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

    @staticmethod
    def _categories_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": category, "callback_data": f"assetcat:{index}"}]
                for index, category in enumerate(CATEGORIES)
            ]
        }

    def _asset_matches(self) -> dict[str, Any]:
        return getattr(self.scanner, "asset_matches", {}) if self.scanner else {}

    async def _show_categories(self, chat_id: int) -> None:
        await self._send_command_reply(
            chat_id,
            "Choose an asset category:",
            command="start",
            reply_markup=self._categories_keyboard(),
            force=True,
        )

    async def _show_category(self, chat_id: int, index_text: str) -> None:
        try:
            category = CATEGORIES[int(index_text)]
        except (ValueError, IndexError):
            await self._show_categories(chat_id)
            return
        matches = self._asset_matches()
        assets = [item for item in CATALOG if item.category == category]
        buttons: list[dict[str, str]] = []
        for item in assets:
            live = matches.get(item.id)
            label = item.name
            payout = getattr(live, "payout", None)
            if payout is not None:
                label += f" · {payout:g}%"
            buttons.append({"text": label, "callback_data": f"asset:{item.id}"})
        rows = [buttons[offset : offset + 2] for offset in range(0, len(buttons), 2)]
        rows.append([{"text": "Back", "callback_data": "assetback"}])
        await self._send_command_reply(
            chat_id,
            f"{category} — choose an asset:",
            command=f"category-{index_text}",
            reply_markup={"inline_keyboard": rows},
            force=True,
        )

    async def _select_asset(self, chat_id: int, asset_id: str) -> None:
        asset = CATALOG_BY_ID.get(asset_id)
        if asset is None:
            await self._show_categories(chat_id)
            return
        select = getattr(self.scanner, "select_asset", None)
        try:
            live = await select(chat_id, asset_id) if callable(select) else None
        except Exception as exc:
            logger.warning(
                "Asset selection failed (%s).", type(exc).__name__
            )
            live = None
        if not live:
            await self._send_command_reply(
                chat_id,
                f"{asset.name} isn't available right now. Pick another asset.",
                command=f"asset-{asset_id}",
                force=True,
            )
            await self._show_category(chat_id, str(CATEGORIES.index(asset.category)))
            return
        payout = f"\nPayout: {live.payout:g}%" if live.payout is not None else ""
        await self._send_command_reply(
            chat_id,
            f"Selected {asset.name}. Signals for this asset are on for this chat."
            f"{payout}",
            command=f"asset-{asset_id}",
            reply_markup=self._keyboard(chat_id),
            force=True,
        )

    async def _pricecheck_reply(self, chat_id: int) -> None:
        subscriber = self.subscribers.subscribers.get(chat_id)
        selected = getattr(subscriber, "selected_asset", None)
        getter = getattr(self.scanner, "price_for_symbol", None)
        price = getter(selected) if selected and callable(getter) else None
        if price is None:
            if not selected:
                await self._send_command_reply(
                    chat_id,
                    "No asset selected. Use /start to choose one.",
                    command="pricecheck",
                    force=True,
                )
                return
            market_asset = next(
                (
                    asset
                    for asset in self._asset_matches().values()
                    if asset.symbol == selected
                ),
                None,
            )
            catalog_asset = CATALOG_BY_ID.get(market_asset.catalog_id) if market_asset else None
            name = catalog_asset.name if catalog_asset else "Your selected asset"
            await self._send_command_reply(
                chat_id,
                f"{name} isn't available right now. Pick another asset.",
                command="pricecheck",
                force=True,
            )
            if market_asset:
                await self._show_category(
                    chat_id, str(CATEGORIES.index(market_asset.category))
                )
            else:
                await self._show_categories(chat_id)
            return
        symbol, value, timestamp = price
        await self._send_command_reply(
            chat_id,
            f"{html.escape(symbol)}: {value:g} at "
            f"{timestamp.astimezone(configured_timezone()).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            command="pricecheck",
            force=True,
        )

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
        await self._send_command_reply(
            chat_id,
            text,
            command="register",
            reply_markup=self._keyboard(chat_id),
        )

    async def _status_reply(self, chat_id: int) -> None:
        subscriber = self.subscribers.subscribers.get(chat_id)
        subscribed = bool(subscriber and subscriber.subscribed)
        scanner_status = self._scanner_status()
        await self._send_command_reply(
            chat_id,
            f"Signals: {'on' if subscribed else 'off'}\n"
            f"Scanner: {scanner_status}\n"
            f"Asset: {self._asset_name(subscriber)}",
            command="status",
            reply_markup=self._keyboard(chat_id),
            force=True,
        )

    def _asset_name(self, subscriber: Any) -> str:
        symbol = getattr(subscriber, "selected_asset", None)
        if not symbol:
            return "not selected"
        return next(
            (
                asset.display_name
                for asset in self._asset_matches().values()
                if asset.symbol == symbol
            ),
            symbol,
        )

    def _scanner_status(self) -> str:
        if bool(getattr(self.scanner, "is_paused", False)):
            return "Starting, waiting for first prices"
        if not getattr(self.scanner, "pairs", {}):
            return "Starting, waiting for first prices"
        age = getattr(self.scanner, "last_tick_age_seconds", None)
        broker = getattr(self.scanner, "broker", None)
        connected_value = getattr(broker, "is_connected", False)
        connected = bool(
            connected_value() if callable(connected_value) else connected_value
        )
        if connected and age is not None and age <= config.WATCHDOG_TICK_MAX_AGE_SECONDS:
            return "Live"
        if age is None:
            return "Starting, waiting for first prices"
        return "Reconnecting"

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
        return {
            "feed_status": "connected" if connected else "disconnected",
            "last_tick_age_seconds": age,
            "selected_assets_streaming": pairs,
            "subscriber_count": subscriber_count,
            "signals_today": today,
            "last_signal_time": last_signal_time,
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
        last_signal_time = metrics["last_signal_time"]
        if last_signal_time:
            parsed_time = datetime.fromisoformat(last_signal_time).astimezone(
                configured_timezone()
            )
            last_signal_time = parsed_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            last_signal_time = "n/a"
        return (
            f"Feed: {metrics['feed_status']}\n"
            f"SSID: {'configured' if os.environ.get('PO_SSID', '').strip() else 'missing'}\n"
            f"Last tick age: {age_text}\n"
            f"Selected assets streaming: {metrics['selected_assets_streaming']}\n"
            f"Subscribers: {metrics['subscriber_count']}\n"
            f"Signals today: {metrics['signals_today']}\n"
            f"Last signal time: {last_signal_time}\n"
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
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (
            (now - signal_time).total_seconds() > 10
            or (signal.valid_until is not None and signal.valid_until <= now)
        )

    async def send(self, signal: Signal) -> str:
        chat_ids = await asyncio.to_thread(
            self.subscribers.subscribed_chat_ids, signal.pair
        )
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
                        signal_time = signal.time
                        if signal_time.tzinfo is None:
                            signal_time = signal_time.replace(tzinfo=timezone.utc)
                        remaining = 10 - (
                            datetime.now(timezone.utc) - signal_time
                        ).total_seconds()
                        if exc.retry_after >= remaining:
                            logger.info(
                                "Signal expired before Telegram's retry window."
                            )
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

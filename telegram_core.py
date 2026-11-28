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
from collections import OrderedDict
from collections.abc import Awaitable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from autotrade_commands import ALL_COMMANDS as AUTOTRADE_COMMAND_NAMES
from autotrade_client import AutotradeError, AutotradeRoster
from autotrade_commands import AutotradeCommands, error_text
from market_assets import CATALOG, CATALOG_BY_ID, CATEGORIES
from notifications import TelegramNotifier, TelegramSendError, format_signal_message
from strategy.model import Signal
from strategy.price_action import setup_label
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


def _stop_balance_text(value) -> str:
    """Optional floor; set with /at_set stop_balance <amount> (0 = off)."""
    if isinstance(value, (int, float)) and value > 0:
        return f"<b>${value:,.2f}</b> (auto-trading pauses at or below it)"
    return "not set (optional: /at_set stop_balance 80)"


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

    async def delete_webhook(self) -> None:
        """Switch this bot to local polling without discarding queued updates."""
        result = await self.call("deleteWebhook", {"drop_pending_updates": False})
        if result is not True:
            raise TelegramSendError("Telegram did not confirm webhook removal.")

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
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", payload)

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback(self, callback_id: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id})

    async def register_commands(
        self,
        admin_id: int | str,
        *,
        extra_public: list[dict[str, str]] | None = None,
        extra_admin: list[dict[str, str]] | None = None,
    ) -> None:
        """extra_* add optional command groups (the auto-trading commands when configured)."""
        default_scope = {"type": "default"}
        admin_scope = {"type": "chat", "chat_id": admin_id}
        public = PUBLIC_COMMANDS + list(extra_public or [])
        admin = public + ADMIN_COMMANDS[len(PUBLIC_COMMANDS):] + list(extra_admin or [])
        await self.call(
            "setMyCommands",
            {"commands": public, "scope": default_scope},
        )
        await self.call(
            "setMyCommands",
            {"commands": admin, "scope": admin_scope},
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
        background_selection: bool = False,
        autotrade: Any = None,
        roster: Any = None,
    ) -> None:
        self.client = client
        # Optional: AutotradeClient for the separate auto-trading service. None = commands say "not set up".
        self.autotrade_enabled = autotrade is not None
        # Who is auto-trading (shared with the fan-out): those users get trades instead of signal cards.
        self.roster = roster if roster is not None else (AutotradeRoster(autotrade) if autotrade else None)
        self.autotrade = AutotradeCommands(autotrade, self._autotrade_send, self.roster)
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
        # One menu message per chat: its message_id is edited in place.
        self.menu_message_ids: dict[int, int] = {}
        self._menu_locks: dict[int, asyncio.Lock] = {}
        self.background_selection = background_selection
        self._background_tasks: set[asyncio.Task[None]] = set()

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
                    await self._render_menu(
                        chat_id,
                        "Sorry, that request could not be completed. Try again.\n\n"
                        "Choose an asset category:",
                        self._categories_keyboard(),
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
                # Answer first so the button stops loading, whatever follows.
                try:
                    await self.client.answer_callback(str(callback_id))
                except TelegramSendError as exc:
                    logger.warning(
                        "Callback answer failed (%s).",
                        exc.status_code or type(exc).__name__,
                    )
            message = callback.get("message")
            message_id = message.get("message_id") if isinstance(message, Mapping) else None
            if chat_id is not None and isinstance(message_id, int):
                self.menu_message_ids[chat_id] = message_id
            sender = callback.get("from")
            sender_id: int | None = None
            if isinstance(sender, Mapping) and sender.get("id") is not None:
                try:
                    sender_id = int(sender["id"])
                except (TypeError, ValueError):
                    sender_id = None
            if chat_id is not None and (data.startswith("mode:") or data.startswith("at:")):
                work = self._mode_or_account_button(chat_id, sender_id, data)
                if self.background_selection:
                    task = asyncio.create_task(work, name=f"mode-{chat_id}")
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                else:
                    await work
                return
            if chat_id is not None and data.startswith("asset:") and self.background_selection:
                task = asyncio.create_task(
                    self._locked_select(chat_id, data.split(":", 1)[1]),
                    name=f"select-asset-{chat_id}",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return
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
                await self._dispatch(
                    data.removeprefix("signals:"), chat_id, from_menu=True
                )
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
        if chat_id is not None and isinstance(text, str) and '42["auth"' in text:
            # A pasted Pocket Option session (SSID) is a secret: delete it from the chat first, then save it
            # encrypted in this user's vault slot (private chats only). The value is never logged.
            if isinstance(message.get("message_id"), int):
                try:
                    await self.client.delete_message(chat_id, message["message_id"])
                except Exception:
                    logger.warning("Could not delete a pasted session message.")
            work = self.autotrade.store_pasted_session(chat_id, sender_id, text)
            if self.background_selection:
                task = asyncio.create_task(work, name=f"autotrade-session-{chat_id}")
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                await work
            return
        if chat_id is None or not isinstance(text, str) or not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        command = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        await self._dispatch(command, chat_id, sender_id=sender_id, args=args)

    async def _dispatch(
        self,
        command: str,
        chat_id: int,
        *,
        sender_id: int | None = None,
        from_menu: bool = False,
        args: str = "",
    ) -> None:
        if command in AUTOTRADE_COMMAND_NAMES:
            if from_menu or sender_id is None:
                return
            is_admin = (
                self.admin_id is not None
                and sender_id == self.admin_id
                and self.admin_chat_id is not None
                and str(chat_id) == str(self.admin_chat_id)
            )
            work = self._autotrade_safe(command, chat_id, sender_id, args, is_admin)
            if self.background_selection:
                # Service calls can be slow (e.g. /at_stop waits for open trades): never block polling.
                task = asyncio.create_task(work, name=f"autotrade-{command}-{chat_id}")
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                await work
            return
        if command in {"start", "stop", "status", "help", "register", "pricecheck"}:
            if command == "start":
                if from_menu:
                    await self._show_categories(chat_id)
                else:
                    await self._restart_menu(chat_id)
            elif command == "stop":
                changed = await asyncio.to_thread(
                    self.subscribers.set_subscribed, chat_id, False
                )
                text = "Signals are off." if changed else "Signals are already off."
                if self.roster is not None and self.roster.is_active(chat_id):
                    self._stop_autotrade_in_background(chat_id)
                    text += " Auto-Trade is stopping; open trades finish first."
                await self._render_menu(chat_id, text, self._keyboard(chat_id))
            elif command == "status":
                if from_menu:
                    await self._render_menu(
                        chat_id, await self._status_text(chat_id), self._keyboard(chat_id)
                    )
                else:
                    await self._status_reply(chat_id)
            elif command == "help":
                await self._send_command_reply(
                    chat_id,
                    "GBot reports informational price-action signals from closed "
                    "1-minute candles. Use /start to choose one asset, /status to "
                    "check your selection, /pricecheck for its latest price, and "
                    "/stop to stop receiving signals. Signals never place trades.\n\n"
                    + (
                        "Optional auto-trading runs on a separate service, starts in Demo "
                        "and only works in a private chat: /autotrade.\n\n"
                        if self.autotrade_enabled
                        else ""
                    )
                    + html.escape(config.PAYOUT_DISCLAIMER, quote=False),
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
                    chat_id, await self._admin_report_with_payouts(), command=command
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
                messages = self.test_signal_provider()
                if asyncio.iscoroutine(messages):
                    messages = await messages
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

    async def _autotrade_send(
        self, chat_id: int, text: str, *, reply_markup: Mapping[str, Any] | None = None
    ) -> None:
        await self.client.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=None)

    async def _autotrade_safe(
        self, command: str, chat_id: int, sender_id: int, args: str, is_admin: bool
    ) -> None:
        try:
            await self.autotrade.handle(command, chat_id, sender_id, args, is_admin=is_admin)
        except Exception as exc:
            logger.error("Auto-trading command failed (%s).", type(exc).__name__)
            try:
                await self._autotrade_send(chat_id, "Sorry, that didn't work. Try again in a minute.")
            except Exception:
                logger.warning("Could not send the auto-trading error reply.")

    def _keyboard(self, chat_id: int) -> dict[str, Any]:
        subscriber = self.subscribers.subscribers.get(chat_id)
        if subscriber is not None and subscriber.subscribed:
            if getattr(subscriber, "selected_asset", None):
                return self._mode_keyboard(chat_id)
            rows = [
                [{"text": "Change asset", "callback_data": "assetback"}],
                [{"text": "Stop", "callback_data": "signals:stop"}],
            ]
        else:
            rows = [
                [{"text": "Choose an asset", "callback_data": "signals:start"}],
                [{"text": "Register", "callback_data": "signals:register"}],
            ]
        return {"inline_keyboard": rows}

    @staticmethod
    def _selected_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Change asset", "callback_data": "assetback"}],
                [{"text": "Stop", "callback_data": "signals:stop"}],
            ]
        }

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

    def _menu_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._menu_locks.get(chat_id)
        if lock is None:
            lock = self._menu_locks[chat_id] = asyncio.Lock()
        return lock

    async def _send_menu(
        self, chat_id: int, text: str, reply_markup: Mapping[str, Any]
    ) -> None:
        result = await self.client.send_message(
            chat_id, text, reply_markup=reply_markup
        )
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        if isinstance(message_id, int):
            self.menu_message_ids[chat_id] = message_id
        else:
            self.menu_message_ids.pop(chat_id, None)

    async def _render_menu(
        self, chat_id: int, text: str, reply_markup: Mapping[str, Any]
    ) -> None:
        """Edit this chat's single menu message; send one new menu if editing fails."""
        message_id = self.menu_message_ids.get(chat_id)
        if message_id is not None:
            try:
                await self.client.edit_message_text(
                    chat_id, message_id, text, reply_markup=reply_markup
                )
                return
            except TelegramSendError as exc:
                if "not modified" in str(exc):
                    return
                logger.info(
                    "Menu edit failed (%s); sending a new menu.",
                    exc.status_code or type(exc).__name__,
                )
        await self._send_menu(chat_id, text, reply_markup)

    async def _restart_menu(self, chat_id: int) -> None:
        """/start: delete the previous menu (the bot's own message) and send a new one."""
        message_id = self.menu_message_ids.pop(chat_id, None)
        if message_id is not None:
            try:
                await self.client.delete_message(chat_id, message_id)
            except TelegramSendError as exc:
                logger.info(
                    "Previous menu could not be deleted (%s).",
                    exc.status_code or type(exc).__name__,
                )
        await self._send_menu(
            chat_id, "Choose an asset category:", self._categories_keyboard()
        )

    async def _show_categories(self, chat_id: int, notice: str = "") -> None:
        await self._render_menu(
            chat_id,
            f"{notice}Choose an asset category:",
            self._categories_keyboard(),
        )

    async def _live_payouts(self) -> dict[str, float]:
        reader = getattr(self.scanner, "live_payouts", None)
        if not callable(reader):
            return {}
        try:
            return await reader()
        except Exception as exc:
            logger.warning("Menu payouts unavailable (%s).", type(exc).__name__)
            return {}

    async def _show_category(
        self, chat_id: int, index_text: str, notice: str = ""
    ) -> None:
        try:
            category = CATEGORIES[int(index_text)]
        except (ValueError, IndexError):
            await self._show_categories(chat_id)
            return
        matches = self._asset_matches()
        payouts = await self._live_payouts()
        assets = [item for item in CATALOG if item.category == category]
        buttons: list[dict[str, str]] = []
        for item in assets:
            live = matches.get(item.id)
            label = item.name
            payout = payouts.get(live.symbol) if live is not None else None
            if payout is not None:
                label += f" · {payout:g}%"
            buttons.append({"text": label, "callback_data": f"asset:{item.id}"})
        rows = [buttons[offset : offset + 2] for offset in range(0, len(buttons), 2)]
        rows.append([{"text": "Back", "callback_data": "assetback"}])
        await self._render_menu(
            chat_id,
            f"{notice}{html.escape(category, quote=False)} — choose an asset:",
            {"inline_keyboard": rows},
        )

    async def _locked_select(self, chat_id: int, asset_id: str) -> None:
        async with self._menu_lock(chat_id):
            try:
                await self._select_asset(chat_id, asset_id)
            except Exception as exc:
                logger.error("Asset selection failed (%s).", type(exc).__name__)
                try:
                    await self._show_categories(
                        chat_id, "Sorry, that request could not be completed. Try again.\n\n"
                    )
                except TelegramSendError:
                    logger.warning("Could not update the menu after an error.")

    async def wait_idle(self) -> None:
        while self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    async def _select_asset(self, chat_id: int, asset_id: str) -> None:
        asset = CATALOG_BY_ID.get(asset_id)
        if asset is None:
            await self._show_categories(chat_id)
            return
        name = html.escape(asset.name, quote=False)
        category_index = str(CATEGORIES.index(asset.category))
        await self._render_menu(
            chat_id,
            f"Checking {name}…",
            {"inline_keyboard": [[{"text": "Back", "callback_data": "assetback"}]]},
        )
        select = getattr(self.scanner, "select_asset", None)
        try:
            live = await select(chat_id, asset_id) if callable(select) else None
        except Exception as exc:
            logger.warning(
                "Asset selection failed (%s).", type(exc).__name__
            )
            live = None
        if not live:
            await self._show_category(
                chat_id, category_index, f"{name} isn't available right now.\n\n"
            )
            return
        await self._render_mode_menu(chat_id, name)

    # ---- mode: 📡 Receive Signals vs 🤖 Auto-Trade -------------------------------------------------
    def _autotrade_allowed(self, chat_id: int) -> bool:
        """Auto-trading is personal: only in a private chat (chat id == Telegram user id)."""
        return self.autotrade_enabled and int(chat_id) > 0

    def _is_auto(self, chat_id: int) -> bool:
        return self.roster is not None and self.roster.is_active(chat_id)

    def _mode_keyboard(self, chat_id: int) -> dict[str, Any]:
        auto = self._is_auto(chat_id)
        rows: list[list[dict[str, str]]] = []
        if self._autotrade_allowed(chat_id):
            rows.append([
                {"text": "📡 Signals" + ("" if auto else " ✅"), "callback_data": "mode:signals"},
                {"text": "🤖 Auto Trade ✅" if auto else "🤖 Auto Trade", "callback_data": "mode:autotrade"},
            ])
            if auto:
                rows.append([{"text": "💰 Balance", "callback_data": "at:balance"},
                             {"text": "📊 Stats", "callback_data": "at:stats"}])
        rows.append([
            {"text": "Back", "callback_data": "assetback"},
            {"text": "Stop", "callback_data": "signals:stop"},
        ])
        return {"inline_keyboard": rows}

    def _selected_name(self, chat_id: int) -> str | None:
        subscriber = self.subscribers.subscribers.get(chat_id)
        if subscriber is None or not getattr(subscriber, "selected_asset", None):
            return None
        return html.escape(self._asset_name(subscriber), quote=False)

    async def _render_mode_menu(self, chat_id: int, name: str, notice: str = "", account: str = "") -> None:
        if self._is_auto(chat_id):
            text = (
                f"🤖 Auto-Trade is on for {name}{f' ({account})' if account else ''}.\n"
                "Signals for this asset are placed on your Pocket Option account automatically. "
                "You get a message when each trade opens and when it closes (win/loss, profit, balance). "
                "Signal cards are paused while Auto-Trade is on."
            )
        else:
            text = (
                f"Selected {name}.\n📡 Signals is on: you get signal cards "
                "(Setup, direction, expiration, payout). No trades are placed."
            )
            if self._autotrade_allowed(chat_id):
                text += "\n🤖 Auto Trade places these signals on your Pocket Option account instead."
        await self._render_menu(chat_id, f"{notice}{text}", self._mode_keyboard(chat_id))

    def _stop_autotrade_in_background(self, chat_id: int) -> None:
        if self.roster is not None:
            self.roster.discard(chat_id)        # signal cards resume at once; the bot drains in the background
        task = asyncio.create_task(self.autotrade.disable(int(chat_id)), name=f"autotrade-stop-{chat_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _mode_or_account_button(self, chat_id: int, sender_id: int | None, data: str) -> None:
        async with self._menu_lock(chat_id):
            try:
                await self._handle_mode_button(chat_id, sender_id, data)
            except TelegramSendError:
                logger.warning("Could not update the menu after a mode button.")
            except Exception as exc:
                logger.error("Mode button failed (%s).", type(exc).__name__)
                try:
                    await self._show_categories(
                        chat_id, "Sorry, that request could not be completed. Try again.\n\n"
                    )
                except TelegramSendError:
                    logger.warning("Could not update the menu after an error.")

    async def _handle_mode_button(self, chat_id: int, sender_id: int | None, data: str) -> None:
        private = sender_id is not None and int(sender_id) == int(chat_id)
        if data.startswith("at:") and not data.startswith(("at:stake:", "at:smode:", "at:go")):
            command, args = {"at:balance": ("at_balance", ""), "at:stats": ("at_stats", ""),
                             "at:demo": ("at_demo", ""), "at:live": ("at_live", ""),
                             "at:liveconfirm": ("at_live", "confirm"),
                             "at:login": ("at_login", ""),
                             "at:session:demo": ("at_session", "demo"),
                             "at:session:real": ("at_session", "real")}.get(data, ("", ""))
            if data.startswith("at:set:") and data.count(":") == 3:    # /at_set menu button
                _, _, field, value = data.split(":")
                command, args = "at_set", f"{field} {value}"
            if command and private:
                await self._autotrade_safe(command, chat_id, int(sender_id), args, False)
            return
        name = self._selected_name(chat_id)
        if name is None:
            await self._show_categories(chat_id, "Choose an asset first.\n\n")
            return
        if data == "mode:signals":
            was_auto = self._is_auto(chat_id)
            if self._autotrade_allowed(chat_id) and private:
                self._stop_autotrade_in_background(chat_id)    # also removes the chat from the roster now
            await asyncio.to_thread(self.subscribers.set_subscribed, chat_id, True)
            notice = "Auto-Trade is stopping; open trades finish first.\n\n" if was_auto else ""
            await self._render_mode_menu(chat_id, name, notice)
            return
        if not self._autotrade_allowed(chat_id) or not private:
            await self._render_mode_menu(
                chat_id, name, "Auto-Trade only works in a private chat with the bot.\n\n"
                if self.autotrade_enabled else "Auto-Trade isn't set up on this bot yet.\n\n")
            return
        if data == "mode:autotrade":
            if self._is_auto(chat_id):
                await self._render_mode_menu(chat_id, name)
                return
            await self._render_autotrade_setup(chat_id, name)
            return
        if data.startswith("at:stake:"):
            try:
                value = float(data.removeprefix("at:stake:"))
                _, note = await self.autotrade.set_stake(int(chat_id), value)
            except (ValueError, AutotradeError) as exc:
                text = error_text(exc) if isinstance(exc, AutotradeError) else "That amount isn't available."
                await self._render_autotrade_setup(chat_id, name, f"{html.escape(text, quote=False)}\n\n")
                return
            await self._render_autotrade_setup(chat_id, name, f"✅ Trade amount set to ${value:.2f}.\n"
                                               + (f"{html.escape(note, quote=False)}\n" if note else "") + "\n")
            return
        if data.startswith("at:smode:"):
            mode = data.removeprefix("at:smode:")
            if mode not in ("fixed", "martingale"):
                return
            try:
                await self.autotrade.client.update_settings(int(chat_id), {"stake_mode": mode})
            except AutotradeError as exc:
                await self._render_autotrade_setup(chat_id, name, f"⚠️ {html.escape(error_text(exc), quote=False)}\n\n")
                return
            label = "Martingale (2x, 4x, then reset)" if mode == "martingale" else "Fixed"
            await self._render_autotrade_setup(chat_id, name, f"✅ Trade mode set to {label}.\n\n")
            return
        if data == "at:go":
            await self._start_autotrade(chat_id, name)

    async def _render_autotrade_setup(self, chat_id: int, name: str, notice: str = "") -> None:
        """🤖 Start Auto-Trade: reuse the saved session (no login prompt when connected), else ask to log in."""
        try:
            state = await self.autotrade.setup_state(int(chat_id))
        except AutotradeError as exc:
            await self._render_mode_menu(chat_id, name, f"{html.escape(error_text(exc), quote=False)}\n\n")
            return
        account = "💵 REAL" if state["kind"] == "real" else "🧪 DEMO"
        switch_row = [{"text": "🧪 Demo" + (" ✅" if state["kind"] == "demo" else ""), "callback_data": "at:demo"},
                      {"text": "💵 Real" + (" ✅" if state["kind"] == "real" else ""), "callback_data": "at:live"}]
        if not state["connected"]:
            which = "Real" if state["kind"] == "real" else "Demo"
            rows = list(self.autotrade.login_markup(int(chat_id))["inline_keyboard"])
            rows += [switch_row, [{"text": "Back", "callback_data": "assetback"}]]
            await self._render_menu(
                chat_id,
                f"{notice}⚠️ <b>Account not connected</b>\nConnect your Pocket Option {which} account first: "
                f"tap a button below, then tap 🤖 Auto Trade again.\n\nAsset: {name} · Account: {account}",
                {"inline_keyboard": rows})
            return
        settings = state["settings"]
        try:
            stake = float(settings.get("stake", 1.0))
        except (TypeError, ValueError):
            stake = 1.0
        smode = str(settings.get("stake_mode", "fixed"))
        balance = state["balance"]
        balance_line = f"Balance: ${balance:,.2f}" if balance is not None else "Balance: checked before you start"
        text = (f"{notice}🤖 <b>Auto-Trade setup</b> · {name}\n"
                f"Account: <b>{account}</b> (connected ✅)\n{balance_line}\n"
                f"Trade amount: <b>${stake:.2f}</b>\n"
                f"Trade mode: <b>{'Martingale (2x, 4x, then reset)' if smode == 'martingale' else 'Fixed'}</b>\n"
                f"Stop Balance: {_stop_balance_text(settings.get('stop_balance'))}\n\n"
                "1. Pick a trade amount  2. Pick a trade mode  3. Start")
        stake_buttons = [r for r in self.autotrade.stake_rows(stake) if not any(
            b["callback_data"] in ("mode:signals", "menu:main") for b in r)]
        rows = [switch_row, *stake_buttons,
                [{"text": "Fixed" + (" ✅" if smode != "martingale" else ""), "callback_data": "at:smode:fixed"},
                 {"text": "Martingale" + (" ✅" if smode == "martingale" else ""), "callback_data": "at:smode:martingale"}],
                [{"text": f"🚀 Start Auto Trade · ${stake:.2f}", "callback_data": "at:go"}],
                [{"text": "Back", "callback_data": "assetback"}]]
        await self._render_menu(chat_id, text, {"inline_keyboard": rows})

    async def _start_autotrade(self, chat_id: int, name: str) -> None:
        if self._is_auto(chat_id):
            await self._render_mode_menu(chat_id, name)
            return
        await self._render_menu(chat_id, f"Checking your balance and starting 🤖 Auto-Trade for {name}…",
                                {"inline_keyboard": [[{"text": "Back", "callback_data": "assetback"}]]})
        try:   # never start on an account that cannot cover the stake
            state = await self.autotrade.setup_state(int(chat_id))
            stake = float(state["settings"].get("stake", 1.0))
            fresh = await self.autotrade.fresh_balance(int(chat_id))
        except AutotradeError as exc:
            await self._render_autotrade_setup(chat_id, name, f"{html.escape(error_text(exc), quote=False)}\n\n")
            return
        if not state["connected"]:
            await self._render_autotrade_setup(chat_id, name)
            return
        balance = fresh.get("balance")
        if isinstance(balance, (int, float)) and float(balance) < stake:
            await self._render_autotrade_setup(
                chat_id, name, f"⚠️ Your balance ${float(balance):,.2f} is below the ${stake:.2f} trade amount. "
                "Pick a lower amount or top up your account.\n\n")
            return
        started, detail, markup = await self.autotrade.enable_follow(int(chat_id))
        if started:
            await asyncio.to_thread(self.subscribers.set_subscribed, chat_id, True)
            await self._render_mode_menu(chat_id, name, account=detail)
            return
        rows = list((markup or {}).get("inline_keyboard", [])) + self._mode_keyboard(chat_id)["inline_keyboard"]
        await self._render_menu(chat_id, f"{html.escape(detail, quote=False)}\n\nAsset: {name}",
                                {"inline_keyboard": rows})

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
        await self._render_menu(chat_id, text, self._keyboard(chat_id))

    async def _payout_line(self, symbol: str | None) -> str:
        account = config.account_type()
        reader = getattr(self.scanner, "live_payout", None)
        payout = await reader(symbol) if symbol and callable(reader) else None
        value = f"{payout:g}%" if payout is not None else "unavailable"
        return f"Payout: {value} ({account} account)"

    async def _status_text(self, chat_id: int) -> str:
        subscriber = self.subscribers.subscribers.get(chat_id)
        subscribed = bool(subscriber and subscriber.subscribed)
        symbol = getattr(subscriber, "selected_asset", None)
        mode = ""
        if subscribed and self._autotrade_allowed(chat_id):
            mode = f"Mode: {'🤖 Auto-Trade' if self._is_auto(chat_id) else '📡 Receive Signals'}\n"
        return (
            f"Signals: {'on' if subscribed else 'off'}\n"
            f"{mode}"
            f"Scanner: {self._scanner_status()}\n"
            f"Asset: {html.escape(self._asset_name(subscriber), quote=False)}\n"
            f"{await self._payout_line(symbol)}"
        )

    async def _status_reply(self, chat_id: int) -> None:
        await self._send_command_reply(
            chat_id,
            await self._status_text(chat_id),
            command="status",
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

    async def _admin_report_with_payouts(self) -> str:
        report = self._admin_report()
        pairs = getattr(self.scanner, "pairs", {}) if self.scanner else {}
        if not pairs:
            return report + f"\nPayouts ({config.account_type()}): no assets streaming"
        payouts = await self._live_payouts()
        parts = []
        for symbol, state in sorted(pairs.items()):
            name = getattr(getattr(state, "asset", None), "display_name", symbol)
            value = payouts.get(symbol)
            parts.append(f"{name} {value:g}%" if value is not None else f"{name} unavailable")
        return report + f"\nPayouts ({config.account_type()}): " + ", ".join(parts)

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
            f"Account: {config.account_type()}\n"
            f"Last tick age: {age_text}\n"
            f"Selected assets streaming: {metrics['selected_assets_streaming']}\n"
            f"Subscribers: {metrics['subscriber_count']}\n"
            f"Signals today: {metrics['signals_today']}\n"
            f"Last signal time: {last_signal_time}\n"
            f"Tick-to-signal latency: {scan_latency_text}\n"
            f"Signal-to-Telegram latency: {delivery_latency_text}\n"
            f"Uptime: {metrics['uptime_seconds']}s"
        )

MAX_TRACKED_DISPATCHES = 64


class SubscriberFanoutNotifier:
    """Deliver each signal to active subscriber chats with global throttling."""

    def __init__(
        self,
        client: TelegramBotClient,
        subscribers: SubscriberStore,
        *,
        max_messages_per_second: int = 25,
        sleep: Callable[[float], Any] = asyncio.sleep,
        autotrade: Any | None = None,
        max_parallel_autotrade: int = 10,
        roster: Any = None,
    ) -> None:
        if max_messages_per_second <= 0:
            raise ValueError("max_messages_per_second must be positive")
        self.client = client
        self.subscribers = subscribers
        self.autotrade = autotrade
        # Only chats on the service's "who is auto-trading" list get hand-offs (and no card).
        self.roster = roster if roster is not None else (AutotradeRoster(autotrade) if autotrade else None)
        self._autotrade_slots = asyncio.Semaphore(max(1, max_parallel_autotrade))
        self._autotrade_tasks: set[asyncio.Task] = set()
        # signal id -> hand-off task (result: the chats that got the signal as a trade instead of a card)
        self._dispatches: "OrderedDict[str, asyncio.Task]" = OrderedDict()
        self.autotrade_accepted = 0
        self.autotrade_handoffs = 0
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

    @staticmethod
    def autotrade_payload(signal: Signal) -> dict[str, Any]:
        signal_time = signal.time if signal.time.tzinfo else signal.time.replace(tzinfo=timezone.utc)
        valid_until = signal.valid_until or (signal_time + timedelta(seconds=10))
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        return {
            "signal_id": signal.id,
            "asset": signal.pair,
            "direction": signal.direction.upper(),
            "expiry_seconds": 60,
            "setup": setup_label(signal.setup)[:48],     # card name, e.g. "Uptrend Pullback"
            "asset_name": str(getattr(signal, "asset_name", "") or "")[:48],
            "valid_until": valid_until.isoformat(),
        }

    async def _mirror_one(self, chat_id: Any, payload: dict[str, Any], signal: Signal | None = None) -> None:
        async with self._autotrade_slots:
            try:
                result = await self.autotrade.submit_signal(chat_id, payload)
            except Exception as exc:   # service down/unauthorised: never affects Telegram delivery
                logger.info("Auto-trade signal hand-off failed (%s).", getattr(exc, "code", type(exc).__name__))
                result = {"accepted": False, "reason": "unreachable"}
            if isinstance(result, dict) and result.get("accepted"):
                self.autotrade_accepted += 1
                return
            reason = result.get("reason") if isinstance(result, dict) else None
            if reason == "follow_signals_off" and self.roster is not None:
                self.roster.discard(chat_id)
            elif reason == "not_running" and self.roster is not None:
                # A just-started worker reports not_running while connecting; keep Auto-Trade mode.
                release = getattr(self.roster, "release", None)
                (release or self.roster.discard)(chat_id)        # the bot stopped: back to signal cards (Signal Mode)
            if reason in ("not_running", "follow_signals_off", "unreachable", "trading_halted") and signal is not None:
                # Nothing was traded: a user who chose this asset still gets the signal card.
                try:
                    chose = int(chat_id) in set(await asyncio.to_thread(
                        self.subscribers.subscribed_chat_ids, signal.pair))
                except Exception:
                    chose = False
                if chose:
                    await self._send_card_late(chat_id, signal)

    async def _send_card_late(self, chat_id: Any, signal: Signal) -> None:
        if self._is_stale(signal):
            return
        try:
            await self._throttle()
            await self.client.send_message(chat_id, format_signal_message(signal))
        except Exception as exc:
            logger.info("Fallback signal card failed (%s).", type(exc).__name__)

    def mirror_to_autotrade(self, chat_ids: Any, signal: Signal) -> int:
        """Start background hand-offs for auto-trading private chats (chat id == Telegram user id)."""
        if self.autotrade is None:
            return 0
        payload = self.autotrade_payload(signal)
        started = 0
        for chat_id in chat_ids:
            try:
                if int(chat_id) <= 0:     # groups/channels are not personal accounts
                    continue
            except (TypeError, ValueError):
                continue
            task = asyncio.create_task(self._mirror_one(chat_id, payload, signal), name=f"autotrade-signal-{chat_id}")
            self._autotrade_tasks.add(task)
            task.add_done_callback(self._autotrade_tasks.discard)
            started += 1
        return started

    def dispatch_autotrade(self, signal: Signal) -> "asyncio.Task | None":
        """Auto-Trade mode: hand this confirmed signal to every auto-trading user's bot at once.

        Called the moment the scanner confirms a signal, before (and independent of) the Telegram card
        queue, so a slow card fan-out can never make a signal expire before it reaches the bots. Every
        active auto-trader gets every confirmed signal (all scanned assets, not only the chart they chose);
        the auto-trade service's payout router then places the best ones. Idempotent per signal id."""
        if self.autotrade is None or self.roster is None:
            return None
        task = self._dispatches.get(signal.id)
        if task is None:
            task = asyncio.create_task(self._dispatch(signal), name=f"autotrade-dispatch-{signal.pair}")
            self._dispatches[signal.id] = task
            while len(self._dispatches) > MAX_TRACKED_DISPATCHES:
                self._dispatches.popitem(last=False)
        return task

    async def _dispatch(self, signal: Signal) -> set[int]:
        if self.roster.stale:
            await self.roster.refresh(timeout=2.0)
        auto_ids: set[int] = set()
        for chat_id in self.roster.active_ids():
            try:
                if int(chat_id) > 0:          # private chats only: groups/channels are not accounts
                    auto_ids.add(int(chat_id))
            except (TypeError, ValueError):
                continue
        self.autotrade_handoffs += self.mirror_to_autotrade(sorted(auto_ids), signal)
        return auto_ids

    async def send(self, signal: Signal) -> str:
        """Signal Mode delivery: the card goes to subscribers of this asset who are not auto-trading."""
        auto_ids: set[int] = set()
        task = self.dispatch_autotrade(signal)      # already started by the scanner callback in production
        if task is not None:
            try:
                auto_ids = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Auto-trade dispatch failed (%s).", type(exc).__name__)
        chat_ids = await asyncio.to_thread(
            self.subscribers.subscribed_chat_ids, signal.pair
        )
        sent = 0
        text = format_signal_message(signal)
        for chat_id in [c for c in chat_ids if c not in auto_ids]:
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
        prepared = False
        while not stop_event.is_set():
            try:
                if not prepared:
                    await self.client.delete_webhook()
                    prepared = True
                    logger.info("Telegram long polling started on this host.")
                await self.poll_once()
                backoff = self.min_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = exc.status_code if isinstance(exc, TelegramSendError) else None
                if status in (401, 403, 409):
                    logger.error("Telegram polling cannot continue (HTTP %s); check the token and stop duplicate pollers.", status)
                    raise
                logger.warning("Telegram polling failed (%s); retrying.", status or type(exc).__name__)
                retry_after = exc.retry_after if isinstance(exc, TelegramSendError) else None
                delay = max(backoff, retry_after or 0)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                backoff = min(self.max_backoff, max(self.min_backoff, backoff * 2))

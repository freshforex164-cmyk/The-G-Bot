"""Hierarchical Telegram presentation; shared transport and delivery remain in telegram_core."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from typing import Any

import telegram_core as core
from autotrade_commands import ALL_COMMANDS, AUTOTRADE_ADMIN_COMMANDS
from market_assets import CATALOG, CATEGORIES
from notifications import TelegramSendError

SubscriberFanoutNotifier = core.SubscriberFanoutNotifier
TelegramPollingService = core.TelegramPollingService
configured_timezone = core.configured_timezone
COMMAND_REPLY_COOLDOWN_SECONDS = core.COMMAND_REPLY_COOLDOWN_SECONDS
PUBLIC_COMMANDS = [
    {"command": "start", "description": "Open G-Bot"},
    {"command": "stop", "description": "Stop receiving signals"},
]
ADMIN_ONLY_COMMANDS = core.ADMIN_ONLY_COMMANDS | {"at_halt", "at_resume"}
ADMIN_COMMANDS = PUBLIC_COMMANDS + core.ADMIN_COMMANDS[len(core.PUBLIC_COMMANDS):] + AUTOTRADE_ADMIN_COMMANDS
WELCOME_TEXT = "<b>G-Bot · Goodness AI Trader</b>\nClosed 1-minute price-action signals. Choose your next action."
logger = logging.getLogger("gbot.navigation")
HELP_TEXT = ("<b>Support / Help</b>\n"
             "1. Tap <b>Choose Assets</b>, pick a group, then an asset.\n"
             "2. Tap <b>Signals</b> to get trade alerts, or <b>Auto Trade</b> to let G-Bot trade for you.\n"
             "3. For Auto Trade: pick Demo or Real, log in (or paste your SSID), choose an amount and a mode, "
             "then tap <b>Start Auto Trade</b>.\n\n"
             "<b>Fixed</b>: same amount every trade. <b>Martingale</b>: after a loss the next trade is 2x, "
             "then 4x, then back to your amount (needs $20+ balance).\n"
             "Signals use closed 1-minute candles and 1-minute expiry. Payouts may vary.\n"
             "Trading carries risk. Practise on Demo first.")
WELCOME_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "welcome.jpg"


def configured_admin_ids(primary: int | None = None) -> frozenset[int]:
    values = {primary} if primary is not None else set()
    for raw in os.environ.get("ADMIN_IDS", "").split(","):
        if raw.strip():
            try:
                values.add(int(raw.strip()))
            except ValueError:
                raise ValueError("ADMIN_IDS must be comma-separated Telegram user IDs.") from None
    return frozenset(values)


def keyboard(*rows: list[tuple[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


class TelegramBotClient(core.TelegramBotClient):
    async def send_photo(self, chat_id, photo, caption, *, reply_markup=None):
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                   "reply_markup": reply_markup or {}}
        if isinstance(photo, Path):
            return await asyncio.to_thread(self._upload_photo_sync, photo, payload)
        return await self.call("sendPhoto", {**payload, "photo": photo})

    def _upload_photo_sync(self, photo, payload):
        """Upload the bundled JPEG; no external asset host or working directory needed."""
        boundary = "gbot-" + uuid.uuid4().hex
        try:
            parts = []
            for name, value in payload.items():
                value = json.dumps(value) if isinstance(value, dict) else str(value)
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                              f'\r\n\r\n{value}\r\n').encode())
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                          'filename="welcome.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode())
            parts.extend([photo.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self._token}/sendPhoto", data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Accept": "application/json"}, method="POST")
            with self._opener(request, timeout=self.request_timeout) as response:
                status = response.getcode()
                body = response.read()
        except urllib.error.HTTPError as exc:
            core.TelegramNotifier._raise_api_error(exc.code, exc.read())
            raise TelegramSendError("Telegram photo upload failed.") from None
        except Exception as exc:
            raise TelegramSendError(f"Telegram photo upload failed ({type(exc).__name__}).") from None
        try:
            result = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramSendError("Telegram returned an unreadable photo response.") from None
        if not isinstance(result, dict):
            raise TelegramSendError("Telegram returned an unexpected photo response.")
        if not 200 <= status < 300 or not result.get("ok"):
            core.TelegramNotifier._raise_api_error(status, body)
        return result.get("result")

    async def edit_message_caption(self, chat_id, message_id, caption, *, reply_markup=None):
        return await self.call("editMessageCaption", {"chat_id": chat_id, "message_id": message_id,
                    "caption": caption, "parse_mode": "HTML", "reply_markup": reply_markup or {}})

    async def register_commands(self, admin_id, *, extra_public=None, extra_admin=None):
        # Public users get a single entry point; admin scopes never leak into the default scope.
        await self.call("setMyCommands", {"commands": PUBLIC_COMMANDS, "scope": {"type": "default"}})
        ids = configured_admin_ids(int(admin_id))
        for uid in ids:
            await self.call("setMyCommands", {"commands": ADMIN_COMMANDS,
                                             "scope": {"type": "chat", "chat_id": uid}})


class TelegramCommandHandler(core.TelegramCommandHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.admin_ids = configured_admin_ids(self.admin_id)
        self._photo_menus: set[int] = set()
        self._menu_context: dict[int, str] = {}

    @staticmethod
    def _main_keyboard():
        # Asset-first: auto-trading starts from an asset's menu, so there is no top-level Auto Trading button.
        return keyboard([("Choose Assets", "menu:assets")],
                        [("Support / Help", "menu:help"), ("Register", "signals:register")])

    def _keyboard(self, chat_id):
        return self._main_keyboard()

    @staticmethod
    def _selected_keyboard():
        return keyboard([("Change asset", "menu:assets"), ("Stop", "signals:stop")], [("Back", "menu:main")])

    @staticmethod
    def _categories_keyboard():
        rows = [[(category, f"assetcat:{index}")] for index, category in enumerate(CATEGORIES)]
        return keyboard(*rows, [("Back", "menu:main")])

    async def _render_menu(self, chat_id, text, reply_markup):
        message_id = self.menu_message_ids.get(chat_id)
        if message_id is not None:
            try:
                if chat_id in self._photo_menus:
                    await self.client.edit_message_caption(chat_id, message_id, text, reply_markup=reply_markup)
                else:
                    await self.client.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
                return
            except (TelegramSendError, AttributeError) as exc:
                if "not modified" in str(exc):
                    return
                try:
                    await self.client.delete_message(chat_id, message_id)
                except (TelegramSendError, AttributeError):
                    pass
        self._photo_menus.discard(chat_id)
        await self._send_menu(chat_id, text, reply_markup)

    async def _restart_menu(self, chat_id):
        old = self.menu_message_ids.pop(chat_id, None)
        self._photo_menus.discard(chat_id)
        if old is not None:
            try:
                await self.client.delete_message(chat_id, old)
            except (TelegramSendError, AttributeError):
                pass
        self._menu_context[chat_id] = "main"
        image_url = os.getenv("WELCOME_IMAGE_URL", "").strip()
        photo = image_url if image_url.startswith("https://") else WELCOME_IMAGE_PATH
        if callable(getattr(self.client, "send_photo", None)):
            try:
                result = await self.client.send_photo(chat_id, photo, WELCOME_TEXT,
                                                      reply_markup=self._main_keyboard())
                message_id = result.get("message_id") if isinstance(result, Mapping) else None
                if isinstance(message_id, int):
                    self.menu_message_ids[chat_id] = message_id
                    self._photo_menus.add(chat_id)
                    return
            except TelegramSendError:
                logger.warning("Welcome image unavailable; opening text menu.")
        await self._send_menu(chat_id, WELCOME_TEXT, self._main_keyboard())

    async def _show_category(self, chat_id, index_text, notice=""):
        try:
            index = int(index_text)
            if not 0 <= index < len(CATEGORIES):
                raise ValueError
            category = CATEGORIES[index]
        except (ValueError, TypeError):
            await self._show_categories(chat_id)
            return
        refresh = getattr(self.scanner, "refresh_asset_matches", None)
        try:
            matches = await refresh() if callable(refresh) else self._asset_matches()
        except Exception:
            # A stale cached asset list is not proof that the market is open.
            matches = {}
        payouts = await self._live_payouts()
        buttons = []
        for asset in CATALOG:
            live = matches.get(asset.id)
            if asset.category != category or live is None or not live.is_open:
                continue
            payout = payouts.get(live.symbol)
            if payout is not None and payout <= 0:
                continue
            label = asset.name + (f" · {payout:g}%" if payout is not None else "")
            buttons.append((label, f"asset:{asset.id}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([("Back", "assetback")])
        text = f"{notice}<b>{html.escape(category)}</b>" + (" — choose an asset:" if buttons else " — no available assets.")
        await self._render_menu(chat_id, text, keyboard(*rows))

    def _is_admin(self, sender_id, chat_id):
        allowed_chat = str(chat_id) == str(self.admin_chat_id) if self.admin_chat_id is not None else chat_id == sender_id
        return sender_id in self.admin_ids and allowed_chat

    async def _handle_update(self, update):
        callback = update.get("callback_query")
        if isinstance(callback, Mapping) and str(callback.get("data", "")).startswith("menu:"):
            callback_id = callback.get("id")
            if callback_id is not None:
                await self.client.answer_callback(str(callback_id))
            chat_id = self._update_chat_id(update)
            if chat_id is None:
                return
            sender = callback.get("from", {})
            sender_id = sender.get("id") if isinstance(sender, Mapping) else None
            message = callback.get("message", {})
            message_id = message.get("message_id") if isinstance(message, Mapping) else None
            # Do not let a stale button turn a signal or another message into a menu.
            if message_id != self.menu_message_ids.get(chat_id):
                return
            async with self._menu_lock(chat_id):
                await self._navigate(chat_id, str(callback["data"])[5:], sender_id)
            return
        await super()._handle_update(update)

    async def _navigate(self, chat_id, target, sender_id):
        self._menu_context[chat_id] = target
        if target == "assets":
            await self._show_categories(chat_id)
        elif target == "main":
            if chat_id in self._photo_menus:
                await self._render_menu(chat_id, WELCOME_TEXT, self._main_keyboard())
            else:
                await self._restart_menu(chat_id)
        elif target == "account":
            await self._render_menu(chat_id, await self._status_text(chat_id),
                keyboard([("Connect account", "menu:at_login"), ("Account link", "signals:register")],
                         [("Back", "menu:main")]))
        elif target == "help":
            await self._render_menu(chat_id, HELP_TEXT,
                keyboard([("Signal status", "signals:status"), ("Stop signals", "signals:stop")], [("Back", "menu:main")]))
        elif target == "live":
            if sender_id != chat_id:
                return
            await self._render_menu(chat_id, "<b>Switch to Live?</b>\nTrades use real money. Losses are possible.",
                                    keyboard([("Confirm Live", "menu:live_confirm"), ("Back", "mode:autotrade")]))
        elif target == "live_confirm":
            await self._dispatch("at_live", chat_id, sender_id=sender_id, args="confirm")
        elif target in ALL_COMMANDS - ADMIN_ONLY_COMMANDS:
            await self._dispatch(target, chat_id, sender_id=sender_id)

    async def _autotrade_send(self, chat_id, text, *, reply_markup=None):
        # Service replies reuse the menu; independently delivered signal messages are untouched.
        markup = dict(reply_markup) if reply_markup else keyboard()
        rows = list(markup.get("inline_keyboard", []))
        rows.append([{"text": "Back", "callback_data": "mode:autotrade"},
                     {"text": "🏠 Main Menu", "callback_data": "menu:main"}])
        await self._render_menu(chat_id, html.escape(text, quote=False), {"inline_keyboard": rows})

    async def _dispatch(self, command, chat_id, *, sender_id=None, from_menu=False, args=""):
        if command == "login":
            command = "at_login"
        if command in ADMIN_ONLY_COMMANDS:
            if from_menu or not self._is_admin(sender_id, chat_id):
                return
            if command in {"at_halt", "at_resume"}:
                await self._autotrade_safe(command, chat_id, sender_id, args, True)
            elif command in {"admin", "health"}:
                await self._send_command_reply(chat_id, await self._admin_report_with_payouts(), command=command)
            elif command in {"pause", "resume"}:
                action = getattr(self.scanner, command, None)
                if callable(action):
                    action()
                text = "Signal scanning is paused." if command == "pause" else "Signal scanning has resumed."
                await self._send_command_reply(chat_id, text if callable(action) else "Scanner unavailable.", command=command)
            elif command == "subscribers":
                ids = await asyncio.to_thread(self.subscribers.subscribed_chat_ids)
                await self._send_command_reply(chat_id, f"Active subscribers: {len(ids)}\nChats: " + ", ".join(map(str, ids)), command=command)
            elif command == "testsignal":
                messages = await self.test_signal_provider() if self.test_signal_provider else []
                await self._send_command_reply(chat_id, "\n\n".join(messages) or "Live test-signal pipeline unavailable.", command=command, parse_mode=None)
            return
        if command == "start":
            if from_menu:
                await self._show_categories(chat_id)
            else:
                await self._restart_menu(chat_id)
            return
        if command == "help":
            await self._navigate(chat_id, "help", sender_id)
            return
        await super()._dispatch(command, chat_id, sender_id=sender_id, from_menu=from_menu, args=args)
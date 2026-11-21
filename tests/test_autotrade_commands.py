"""Telegram auto-trading commands (no network): private-chat only, Demo->Live needs 'confirm',
settings parsing, error replies, admin-only kill switch, and short-lived service tokens."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from autotrade_client import AutotradeClient, AutotradeError, make_jwt
from autotrade_commands import AUTOTRADE_ADMIN_COMMANDS, AUTOTRADE_COMMANDS, NOT_SET_UP, PRIVATE_ONLY
from subscriber_store import SubscriberStore
from telegram_bot import TelegramBotClient, TelegramCommandHandler

SECRET = "s" * 40
USER = 555
ADMIN = 900
ADMIN_CHAT = -100900


def decode(token: str) -> dict:
    header, payload, signature = token.split(".")
    expected = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "==") == expected, "bad signature"
    return json.loads(base64.urlsafe_b64decode(payload + "=="))


class Chat:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict | None]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def last(self) -> str:
        return self.sent[-1][1]


class FakeService:
    """Stands in for AutotradeClient; records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: AutotradeError | None = None
        self.stop_gate: asyncio.Event | None = None
        self.settings_data = {"mode": "demo", "stake": 1.0, "expiry_seconds": 60, "max_concurrent": 2,
                              "cooldown_seconds": 5, "daily_stop_loss": 10.0, "daily_take_profit": 20.0,
                              "min_payout": 80, "strategy": "price_action", "symbol_otc": "EURUSD_otc",
                              "follow_signals": True}
        self.balance_data = {"account": "demo", "balance": 9.0, "fresh": True, "at": "2026-11-21T08:00:00+00:00"}

    def _record(self, *call):
        self.calls.append(call)
        if self.fail:
            raise self.fail

    sessions = {"demo": {"status": "missing"}, "real": {"status": "missing"}}

    async def auth_status(self, uid):
        self._record("auth_status", uid)
        return self.sessions

    async def submit_ssid(self, uid, ssid):
        self._record("submit_ssid", uid, ssid)
        return {"kind": "real" if '"isDemo":0' in ssid else "demo", "verified": True, "status": "active"}

    def login_link(self, uid):
        return "https://trade.example/login#t=abc"

    async def stats(self, uid):
        self._record("stats", uid)
        return {"mode": self.settings_data["mode"], "bot": {"running": False, "status": "stopped"},
                "sessions": {"demo": {"status": "active"}, "real": {"status": "active"}},
                "today": {"trades": 0}, "all_time": {"trades": 0}, "settings": self.settings_data,
                "recent_trades": []}

    async def start(self, uid):
        self._record("start", uid)
        return {}

    async def stop(self, uid):
        self._record("stop", uid)
        if self.stop_gate:
            await self.stop_gate.wait()
        return {}

    async def settings(self, uid):
        self._record("settings", uid)
        return self.settings_data

    async def update_settings(self, uid, patch):
        self._record("update_settings", uid, patch)
        return {**self.settings_data, **patch}

    async def revoke(self, uid):
        self._record("revoke", uid)

    async def balance(self, uid, fresh=True):
        self._record("balance", uid, fresh)
        return self.balance_data

    async def active_users(self):
        self._record("active_users")
        return set()

    async def halt(self, uid):
        self._record("halt", uid)

    async def resume(self, uid):
        self._record("resume", uid)


def message(text: str, chat_id: int = USER, sender_id: int = USER) -> dict:
    return {"message": {"chat": {"id": chat_id}, "from": {"id": sender_id}, "text": text}}


class AutotradeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.store = SubscriberStore(database_url="", file_path=Path(self.tmp.name) / "s.json")
        self.chat = Chat()
        self.service = FakeService()
        self.handler = self.make_handler(self.service)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def make_handler(self, service, background=False):
        return TelegramCommandHandler(self.chat, self.store, admin_id=ADMIN, admin_chat_id=ADMIN_CHAT,
                                      autotrade=service, background_selection=background)

    async def send(self, text: str, **kw) -> None:
        await self.handler.handle_update(message(text, **kw))
        await self.handler.wait_idle()

    async def test_not_configured_answers_not_set_up(self):
        self.handler = self.make_handler(None)
        await self.send("/at_start")
        self.assertEqual(self.chat.last(), NOT_SET_UP)

    async def test_commands_only_work_in_a_private_chat(self):
        await self.send("/at_start", chat_id=-1001, sender_id=USER)
        self.assertEqual(self.chat.last(), PRIVATE_ONLY)
        self.assertEqual(self.service.calls, [])

    async def test_live_needs_the_word_confirm(self):
        await self.send("/at_live")
        await self.send("/at_live yes")
        self.assertEqual(self.service.calls, [])
        self.assertIn("/at_live confirm", self.chat.last())
        await self.send("/at_live confirm")
        self.assertEqual(self.service.calls, [("update_settings", USER, {"mode": "live", "confirm_live": True})])

    async def test_demo_switch_needs_no_confirmation(self):
        await self.send("/at_demo")
        self.assertEqual(self.service.calls, [("update_settings", USER, {"mode": "demo"})])

    async def test_set_maps_names_and_types(self):
        for text in ("/at_set stake 2.5", "/at_set expiry 60", "/at_set max_trades 2",
                     "/at_set stop_loss 15", "/at_set take_profit 30", "/at_set cooldown 45"):
            await self.send(text)
        patches = [c[2] for c in self.service.calls]
        self.assertEqual(patches, [{"stake": 2.5}, {"expiry_seconds": 60}, {"max_concurrent": 2},
                                   {"daily_stop_loss": 15.0}, {"daily_take_profit": 30.0},
                                   {"cooldown_seconds": 45}])
        self.assertIsInstance(patches[1]["expiry_seconds"], int)

    async def test_set_rejects_bad_values_without_calling_the_service(self):
        for text in ("/at_set expiry 1.5", "/at_set stake abc", "/at_set stake nan", "/at_set leverage 5"):
            await self.send(text)
        self.assertEqual(self.service.calls, [])

    async def test_expired_session_points_to_at_login(self):
        self.service.fail = AutotradeError(409, "reauth_required", "expired", "real")
        await self.send("/at_start")
        self.assertIn("Real session", self.chat.last())
        self.assertIn("/at_login", self.chat.last())

    async def test_service_down_reply(self):
        self.service.fail = AutotradeError(0, "unreachable")
        await self.send("/at_stats")
        self.assertIn("can't be reached", self.chat.last())

    async def test_login_sends_a_telegram_web_app_button(self):
        await self.send("/at_login")
        markup = self.chat.sent[-1][2]
        self.assertEqual(markup["inline_keyboard"][0][0]["web_app"]["url"], "https://trade.example/login#t=abc")

    async def test_login_reuses_a_saved_session_instead_of_a_new_sign_in(self):
        self.service.sessions = {"demo": {"status": "active"}, "real": {"status": "active"}}
        await self.send("/at_login")
        self.assertIn("Already connected (Demo ✅ · Real ✅)", self.chat.last())
        rows = self.chat.sent[-1][2]["inline_keyboard"]
        self.assertEqual(rows[1][0]["callback_data"], "at:ssid")

    async def test_login_menu_offers_direct_ssid_login(self):
        await self.send("/at_login")
        self.assertEqual(self.chat.sent[-1][2]["inline_keyboard"][1][0]["text"], "🔑 Direct SSID Login")

    async def test_direct_ssid_is_saved_and_the_message_deleted(self):
        ssid = '42["auth",{"session":"abc","isDemo":0,"uid":1,"platform":2}]'
        update = message(f"/at_ssid {ssid}")
        update["message"]["message_id"] = 77
        await self.handler.handle_update(update)
        self.assertIn(("submit_ssid", USER, ssid), self.service.calls)
        self.assertEqual(self.chat.deleted, [(USER, 77)])
        self.assertIn("Real session saved", self.chat.last())

    async def test_ssid_without_value_shows_help(self):
        await self.send("/at_ssid")
        self.assertIn("Direct SSID Login", self.chat.last())
        self.assertFalse(any(c[0] == "submit_ssid" for c in self.service.calls))

    async def test_kill_switch_only_for_admin_in_admin_chat(self):
        await self.send("/at_halt")                                     # normal user
        await self.send("/at_halt", chat_id=ADMIN, sender_id=ADMIN)     # admin, wrong chat
        self.assertEqual(self.service.calls, [])
        await self.send("/at_halt", chat_id=ADMIN_CHAT, sender_id=ADMIN)
        self.assertEqual(self.service.calls, [("halt", ADMIN)])

    async def test_slow_stop_does_not_block_other_users(self):
        self.service.stop_gate = asyncio.Event()
        self.handler = self.make_handler(self.service, background=True)
        await self.handler.handle_update(message("/at_stop"))
        await self.handler.handle_update(message("/at_stats", chat_id=777, sender_id=777))
        for _ in range(50):
            if ("stats", 777) in self.service.calls:
                break
            await asyncio.sleep(0.01)
        self.assertIn(("stats", 777), self.service.calls)
        self.service.stop_gate.set()
        await self.handler.wait_idle()
        self.assertEqual(self.chat.last(), "Your bot is stopped.")


class AutotradeClientTests(unittest.IsolatedAsyncioTestCase):
    def test_tokens_are_short_lived_and_scoped(self):
        client = AutotradeClient("https://svc.example/", SECRET, clock=lambda: 1000)
        api = decode(client.token(USER))
        self.assertEqual((api["sub"], api["exp"]), ("tg:555", 1120))
        self.assertNotIn("scope", api)
        self.assertNotIn("role", api)
        link = client.login_link(USER)
        self.assertTrue(link.startswith("https://svc.example/login#t="))
        login = decode(link.split("#t=", 1)[1])
        self.assertEqual((login["scope"], login["exp"]), ("login", 1900))
        self.assertNotIn("role", decode(client.token(ADMIN, scope="login", admin=True)))
        self.assertEqual(decode(client.token(ADMIN, admin=True))["role"], "admin")

    def test_short_secret_rejected(self):
        with self.assertRaises(ValueError):
            AutotradeClient("https://svc.example", "short")

    def test_from_env_needs_both_variables(self):
        with mock.patch.dict(os.environ, {"AUTOTRADE_API_URL": "https://svc.example"}, clear=False):
            os.environ.pop("AUTOTRADE_JWT_SECRET", None)
            self.assertIsNone(AutotradeClient.from_env())

    async def test_service_errors_are_parsed(self):
        def opener(request, timeout):
            body = json.dumps({"detail": {"error": "reauth_required", "kind": "demo", "reason": "expired"}})
            raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(body.encode()))
        client = AutotradeClient("https://svc.example", SECRET, opener=opener)
        with self.assertRaises(AutotradeError) as ctx:
            await client.start(USER)
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.kind), (409, "reauth_required", "demo"))

        def down(request, timeout):
            raise urllib.error.URLError("refused")
        with self.assertRaises(AutotradeError) as ctx:
            await AutotradeClient("https://svc.example", SECRET, opener=down).stats(USER)
        self.assertEqual(ctx.exception.status, 0)

    def test_make_jwt_matches_hs256(self):
        self.assertEqual(decode(make_jwt(SECRET, {"sub": "x"})), {"sub": "x"})


class RegisterCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_autotrade_commands_registered_only_when_passed(self):
        calls = []

        class Bot(TelegramBotClient):
            async def call(self, method, payload=None):
                calls.append((method, payload))
                return payload["commands"] if method == "setMyCommands" else []

        await Bot("t").register_commands(ADMIN, extra_public=AUTOTRADE_COMMANDS, extra_admin=AUTOTRADE_ADMIN_COMMANDS)
        public = [c["command"] for c in calls[0][1]["commands"]]
        admin = [c["command"] for c in calls[1][1]["commands"]]
        self.assertEqual(public, ["start"])
        self.assertNotIn("at_halt", public)
        self.assertIn("at_halt", admin)
        self.assertIn("at_resume", admin)

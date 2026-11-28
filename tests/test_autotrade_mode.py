"""/start -> category -> asset -> [📡 Receive Signals] or [🤖 Start Auto-Trade], the shared
"who is auto-trading" list, /at_balance, and GBot's scoped service tokens."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autotrade_client import AutotradeClient, AutotradeError, AutotradeRoster
from autotrade_commands import balance_text
from subscriber_store import SubscriberStore
from telegram_bot import TelegramCommandHandler
from tests.menu_fakes import FakeScanner, MenuClient
from tests.test_autotrade_commands import SECRET, FakeService, decode, message

USER = 555
GROUP = -100777


def tap(data: str, chat_id: int = USER, sender_id: int | None = USER) -> dict:
    cb = {"id": "cb1", "data": data, "message": {"message_id": 1, "chat": {"id": chat_id}}}
    if sender_id is not None:
        cb["from"] = {"id": sender_id}
    return {"callback_query": cb}


class StoringScanner(FakeScanner):
    """Like the real scanner: a successful selection stores the asset and turns signals on."""

    def __init__(self, store) -> None:
        super().__init__()
        self.store = store

    async def select_asset(self, chat_id, asset_id):
        asset = await super().select_asset(chat_id, asset_id)
        if asset is not None:
            self.store.set_asset(chat_id, asset.symbol)
        return asset


def buttons(markup: dict | None) -> list[str]:
    return [b["text"] for row in (markup or {}).get("inline_keyboard", []) for b in row]


class ModeMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.store = SubscriberStore(database_url="", file_path=Path(self.tmp.name) / "s.json")
        self.client = MenuClient()
        self.service = FakeService()
        self.service.sessions = {"demo": {"status": "active"}, "real": {"status": "missing"}}
        self.handler = TelegramCommandHandler(self.client, self.store, admin_id=None, scanner=StoringScanner(self.store),
                                              autotrade=self.service)

    async def asyncTearDown(self) -> None:
        await self.handler.wait_idle()
        self.tmp.cleanup()

    async def pick_asset(self, chat_id: int = USER) -> dict:
        await self.handler.handle_update(message("/start", chat_id=chat_id, sender_id=USER))
        await self.handler.handle_update(tap("assetcat:0", chat_id))
        await self.handler.handle_update(tap("asset:aud-cad-otc", chat_id))
        return self.client.menus(chat_id)[-1]

    async def test_after_choosing_an_asset_both_modes_are_offered_signals_first(self):
        menu = await self.pick_asset()
        self.assertIn("📡 Signals ✅", buttons(menu["markup"]))
        self.assertIn("🤖 Auto Trade", buttons(menu["markup"]))
        self.assertIn("No trades are placed", menu["text"])
        self.assertEqual(self.store.subscribers[USER].selected_asset, "AUDCAD_otc")

    async def test_group_chats_only_get_signals(self):
        menu = await self.pick_asset(GROUP)
        self.assertNotIn("🤖 Auto Trade", buttons(menu["markup"]))

    async def start_auto(self) -> None:
        await self.handler.handle_update(tap("mode:autotrade"))
        await self.handler.handle_update(tap("at:go"))

    async def test_start_auto_trade_switches_mode_and_joins_the_auto_trader_list(self):
        await self.pick_asset()
        await self.start_auto()
        menu = self.client.menus(USER)[-1]
        self.assertIn(("start", USER), self.service.calls)
        self.assertTrue(self.handler.roster.is_active(USER))
        self.assertIn("🤖 Auto Trade ✅", buttons(menu["markup"]))
        self.assertIn("📡 Signals", buttons(menu["markup"]))
        self.assertIn("Auto-Trade is on for AUD/CAD OTC (DEMO)", menu["text"])
        self.assertIn("Signal cards are paused", menu["text"])

    async def test_auto_trade_turns_follow_mode_on_first(self):
        self.service.settings_data["follow_signals"] = False
        await self.pick_asset()
        await self.start_auto()
        self.assertIn(("update_settings", USER, {"follow_signals": True}), self.service.calls)

    async def test_without_a_connected_account_the_login_button_is_shown(self):
        await self.pick_asset()
        self.service.fail = AutotradeError(409, "reauth_required", kind="demo")
        await self.handler.handle_update(tap("mode:autotrade"))
        menu = self.client.menus(USER)[-1]
        self.assertIn("Log in to Pocket Option", buttons(menu["markup"]))
        self.assertIn("Account not connected", menu["text"])
        self.assertNotIn(("start", USER), self.service.calls)
        self.assertFalse(self.handler.roster.is_active(USER))

    async def test_receive_signals_leaves_auto_trade(self):
        await self.pick_asset()
        await self.start_auto()
        await self.handler.handle_update(tap("mode:signals"))
        await self.handler.wait_idle()
        self.assertFalse(self.handler.roster.is_active(USER))
        self.assertIn(("stop", USER), self.service.calls)
        menu = self.client.menus(USER)[-1]
        self.assertIn("📡 Signals ✅", buttons(menu["markup"]))
        self.assertIn("Auto-Trade is stopping", menu["text"])

    async def test_mode_buttons_need_the_users_own_tap(self):
        await self.pick_asset()
        await self.handler.handle_update(tap("mode:autotrade", sender_id=None))
        await self.handler.handle_update(tap("at:go", sender_id=None))
        self.assertNotIn(("start", USER), self.service.calls)

    async def test_balance_button_sends_a_fresh_balance(self):
        await self.pick_asset()
        await self.handler.handle_update(tap("at:balance"))
        self.assertIn(("balance", USER, True), self.service.calls)
        texts = [m["text"] for m in self.client.visible[USER].values()]
        self.assertIn("💰 Balance (Demo): 9.00\nUpdated just now.", texts)

    async def test_at_balance_command(self):
        await self.handler.handle_update(message("/at_balance"))
        self.assertIn(("balance", USER, True), self.service.calls)

    async def test_gap_setting_is_no_longer_accepted(self):
        await self.handler.handle_update(message("/at_set gap 0.002"))
        texts = [m["text"] for m in self.client.visible[USER].values()]
        self.assertTrue(any(t.startswith("Unknown setting 'gap'") for t in texts))
        self.assertFalse(any(c[0] == "update_settings" for c in self.service.calls))


class BalanceTextTests(unittest.TestCase):
    def test_saved_balance_when_fresh_read_fails(self):
        text = balance_text({"account": "real", "balance": 12.5, "fresh": False, "at": "2026-11-21T08:05:00+00:00",
                             "error": "broker_unreachable"})
        self.assertIn("Last known balance (Real): 12.50", text)
        self.assertIn("2026-11-21 08:05 UTC", text)
        self.assertIn("Pocket Option couldn't be reached", text)

    def test_no_balance_yet(self):
        self.assertIn("Send /at_login first", balance_text({"account": "demo", "balance": None, "error": "no_session"}))


class ServiceTokenTests(unittest.IsolatedAsyncioTestCase):
    def test_signal_tokens_are_separate_and_short_lived(self):
        client = AutotradeClient("https://svc.example", SECRET, clock=lambda: 1000)
        claims = decode(client.token(USER, scope="signal"))
        self.assertEqual((claims["sub"], claims["scope"], claims["exp"]), ("tg:555", "signal", 1030))
        service = decode(client.token(None, scope="service"))
        self.assertEqual((service["sub"], service["scope"]), ("service:gbot", "service"))

    async def test_submit_signal_uses_a_signal_token(self):
        seen = []

        def opener(request, timeout):
            seen.append(decode(request.get_header("Authorization").split()[1]))
            raise OSError("offline")
        client = AutotradeClient("https://svc.example", SECRET, opener=opener)
        with self.assertRaises(AutotradeError):
            await client.submit_signal(USER, {"signal_id": "x"})
        self.assertEqual(seen[0]["scope"], "signal")

    async def test_roster_keeps_the_last_list_when_the_service_is_down(self):
        class Svc:
            def __init__(self):
                self.result: set[int] | Exception = {1, 2}

            async def active_users(self):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result
        now = [0.0]
        svc = Svc()
        roster = AutotradeRoster(svc, max_age=20, clock=lambda: now[0])
        self.assertTrue(await roster.refresh())
        self.assertEqual(roster.active_ids(), {1, 2})
        now[0] = 30
        svc.result = OSError("down")
        self.assertFalse(await roster.refresh())
        self.assertEqual(roster.active_ids(), {1, 2})


if __name__ == "__main__":
    unittest.main()

"""Asset-first menu, the Auto-Trade setup screen (connection check, Demo/Real, trade amounts, balance
check). Direct SSID login is gone: pasted session codes are deleted and refused."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autotrade_commands import STAKE_OPTIONS
from subscriber_store import SubscriberStore
from telegram_bot import TelegramCommandHandler
from tests.menu_fakes import MenuClient
from tests.test_autotrade_commands import FakeService, message
from tests.test_autotrade_mode import USER, StoringScanner, buttons, tap

DEMO_SSID = '42["auth",{"session":"abc","isDemo":1,"uid":1,"platform":2}]'
REAL_SSID = '42["auth",{"session":"abc","isDemo":0,"uid":1,"platform":2}]'


class RefinementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.store = SubscriberStore(database_url="", file_path=Path(self.tmp.name) / "s.json")
        self.client = MenuClient()
        self.service = FakeService()
        self.service.sessions = {"demo": {"status": "active"}, "real": {"status": "missing"}}
        self.service.settings_data = dict(self.service.settings_data)
        self.handler = TelegramCommandHandler(self.client, self.store, admin_id=None,
                                              scanner=StoringScanner(self.store), autotrade=self.service)

    async def asyncTearDown(self) -> None:
        await self.handler.wait_idle()
        self.tmp.cleanup()

    def last(self) -> dict:
        return self.client.menus(USER)[-1]

    async def open_setup(self) -> dict:
        await self.handler.handle_update(message("/start"))
        await self.handler.handle_update(tap("assetcat:0"))
        await self.handler.handle_update(tap("asset:aud-cad-otc"))
        await self.handler.handle_update(tap("mode:autotrade"))
        return self.last()

    async def test_main_menu_has_no_top_level_auto_trading_button(self):
        await self.handler.handle_update(message("/start"))
        self.assertEqual(buttons(self.last()["markup"]), ["Choose Asset", "Account Info", "Support/Help"])

    async def test_connected_account_gets_the_trade_amount_picker_not_a_login_prompt(self):
        menu = await self.open_setup()
        labels = buttons(menu["markup"])
        for label in ["$1.00 ✅", "$1.50", "$2.00", "$2.50", "$5.00 RISK", "$10.00 RISK",
                      "$25.00", "$50.00", "$100.00", "🛑 Stop", "🏠 Main Menu"]:
            self.assertIn(label, labels)
        self.assertNotIn("Log in to Pocket Option", labels)
        self.assertIn("connected ✅", menu["text"])
        self.assertNotIn(("start", USER), self.service.calls)

    def test_stake_options_are_exactly_the_requested_amounts(self):
        self.assertEqual([v for _, v in STAKE_OPTIONS], [1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0])

    async def test_missing_session_for_the_chosen_mode_asks_to_log_in(self):
        self.service.settings_data["mode"] = "live"          # Real chosen, only Demo is connected
        menu = await self.open_setup()
        self.assertIn("Log in to Pocket Option", buttons(menu["markup"]))
        self.assertIn("Real account first", menu["text"])

    async def test_picking_an_amount_saves_it(self):
        await self.open_setup()
        await self.handler.handle_update(tap("at:stake:2.5"))
        self.assertIn(("update_settings", USER, {"stake": 2.5}), self.service.calls)
        self.assertIn("Trade amount set to $2.50", self.last()["text"])

    async def test_amount_above_the_daily_stop_loss_raises_it_to_the_amount(self):
        await self.open_setup()
        await self.handler.handle_update(tap("at:stake:25"))
        self.assertIn(("update_settings", USER, {"stake": 25.0, "daily_stop_loss": 25.0}), self.service.calls)

    async def test_unlisted_amount_is_refused(self):
        await self.open_setup()
        await self.handler.handle_update(tap("at:stake:7"))
        self.assertFalse(any(c[0] == "update_settings" for c in self.service.calls))

    async def test_start_is_refused_when_the_balance_is_below_the_stake(self):
        self.service.settings_data["stake"] = 10.0
        self.service.balance_data = {"account": "demo", "balance": 9.0, "fresh": True}
        await self.open_setup()
        await self.handler.handle_update(tap("at:go"))
        self.assertNotIn(("start", USER), self.service.calls)
        self.assertIn("below the $10.00 trade amount", self.last()["text"])

    async def test_start_checks_a_fresh_balance_first(self):
        await self.open_setup()
        await self.handler.handle_update(tap("at:go"))
        self.assertLess(self.service.calls.index(("balance", USER, True)), self.service.calls.index(("start", USER)))

    async def test_ssid_menu_buttons_do_nothing(self):
        before = len(self.client.visible.get(USER, {}))
        for data in ("at:ssid", "at:ssid:demo", "at:ssid:real"):
            await self.handler.handle_update(tap(data))
        self.assertEqual(len(self.client.visible.get(USER, {})), before)
        self.assertFalse(any(c[0] == "submit_ssid" for c in self.service.calls))

    async def test_pasted_ssid_frame_is_deleted_then_saved_encrypted(self):
        for text, kind in ((REAL_SSID, "real"), (DEMO_SSID, "demo")):
            update = message(text)
            update["message"]["message_id"] = 88
            await self.handler.handle_update(update)
            self.assertIn(88, self.client.deletes)
            self.client.deletes.clear()
            reply = list(self.client.visible[USER].values())[-1]["text"]
            self.assertIn("session saved", reply)
            self.assertNotIn("abc", reply)
            self.assertIn(("submit_manual_session", USER, kind), self.service.calls)


if __name__ == "__main__":
    unittest.main()

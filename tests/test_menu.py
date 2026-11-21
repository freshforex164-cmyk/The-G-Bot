"""Step 5: one menu message per chat; signals are never edited or deleted."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from strategy.model import Signal
from subscriber_store import SubscriberStore
from telegram_bot import SubscriberFanoutNotifier, TelegramCommandHandler
from tests.menu_fakes import FakeScanner, MenuClient, start, tap


class OneMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = SubscriberStore(database_url="", file_path=Path(self.temp.name) / "s.json")
        self.client = MenuClient()
        self.scanner = FakeScanner(available=("aud-cad-otc",))
        self.handler = TelegramCommandHandler(
            self.client, self.store, admin_id=None, scanner=self.scanner
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def menu_id(self) -> int:
        return self.handler.menu_message_ids[5]

    async def press(self, data: str, callback_id: str = "cb") -> None:
        await self.handler.handle_update(tap(data, self.menu_id(), callback_id=callback_id))


    async def test_unavailable_asset_is_reported_once_inside_the_open_menu(self) -> None:
        await self.handler.handle_update(start())
        await self.press("assetcat:0")
        await self.press("asset:gbp-usd-otc")
        menus = self.client.menus(5)
        self.assertEqual(len(menus), 1)
        self.assertTrue(menus[0]["text"].startswith("GBP/USD OTC isn't available right now."))
        self.assertEqual(menus[0]["text"].count("isn't available"), 1)
        self.assertIn("Back", [b["text"] for row in menus[0]["markup"]["inline_keyboard"] for b in row])

    async def test_start_deletes_the_previous_menu_and_stores_the_new_id(self) -> None:
        await self.handler.handle_update(start())
        first = self.menu_id()
        await self.handler.handle_update(start())
        self.assertIn(first, self.client.deletes)
        self.assertNotEqual(self.menu_id(), first)
        self.assertEqual(len(self.client.menus(5)), 1)

    async def test_failed_edit_sends_exactly_one_new_menu(self) -> None:
        await self.handler.handle_update(start())
        before = self.menu_id()
        self.client.fail_next_edit = True
        await self.press("assetcat:0")
        self.assertNotEqual(self.menu_id(), before)
        self.assertEqual(self.client.next_id - 1, 2)

    async def test_every_callback_is_answered(self) -> None:
        await self.handler.handle_update(start())
        for index, data in enumerate(("assetcat:0", "asset:aud-cad-otc", "assetback", "unknown")):
            await self.press(data, callback_id=f"cb{index}")
        self.assertEqual(self.client.callbacks, ["cb0", "cb1", "cb2", "cb3"])

    async def test_random_tap_sequences_leave_one_menu_and_never_touch_signals(self) -> None:
        fanout = SubscriberFanoutNotifier(self.client, self.store)
        rng = random.Random(7)
        actions = ["start", "assetcat:0", "assetcat:1", "asset:aud-cad-otc",
                   "asset:gbp-usd-otc", "assetback", "signals:stop", "signals:status",
                   "signals:start", "signal"]
        signal_ids: set[int] = set()
        await self.handler.handle_update(start())
        for _ in range(200):
            action = rng.choice(actions)
            if action == "start":
                await self.handler.handle_update(start())
            elif action == "signal":
                self.store.set_asset(5, "AUDCAD_otc")
                before = set(self.client.visible[5])
                await fanout.send(Signal(
                    pair="AUDCAD_otc", direction="BUY", setup="UP_CONTINUATION_BUY",
                    price=1.0, time=datetime.now(timezone.utc), asset_name="AUD/CAD OTC",
                ))
                signal_ids |= set(self.client.visible[5]) - before
            else:
                await self.press(action)
            self.assertEqual(len(self.client.menus(5)), 1, action)
        self.assertTrue(signal_ids)
        self.assertFalse(signal_ids & set(self.client.edits))
        self.assertFalse(signal_ids & set(self.client.deletes))
        self.assertTrue(signal_ids <= set(self.client.visible[5]))


if __name__ == "__main__":
    unittest.main()

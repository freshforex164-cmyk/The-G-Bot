"""Step 4: payouts read from the feed at display/signal time, account type."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import config
from notifications import format_signal_message
from scanner import SignalScanner
from strategy.model import Signal
from subscriber_store import SubscriberStore
from telegram_bot import TelegramCommandHandler
from tests.menu_fakes import FAKE_DEMO_SSID, FAKE_REAL_SSID, FakeScanner, MenuClient, start, tap


def signal(payout):
    return Signal(
        pair="AUDCAD_otc",
        direction="BUY",
        setup="UP_CONTINUATION_BUY",
        price=1.0,
        time=datetime.now(timezone.utc),
        asset_name="AUD/CAD OTC",
        payout=payout,
    )


class AccountTypeTests(unittest.TestCase):
    def test_account_type_comes_from_the_ssid(self) -> None:
        self.assertEqual(config.account_type(FAKE_DEMO_SSID), "DEMO")
        self.assertEqual(config.account_type(FAKE_REAL_SSID), "REAL")
        self.assertEqual(config.account_type(""), "UNKNOWN")



class SignalTimePayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_payout_is_read_from_the_feed_when_the_signal_fires(self) -> None:
        reads = iter([81.0, 88.0])

        class Broker:
            async def payout(self, symbol):
                return next(reads)

        published = []
        scanner = SignalScanner(Broker(), SimpleNamespace(subscribers={}))
        scanner._publish_signal = published.append
        await scanner._publish_with_live_payout(signal(None))
        await scanner._publish_with_live_payout(signal(None))
        self.assertEqual([item.payout for item in published], [81.0, 88.0])

    async def test_unreadable_payout_is_unavailable_not_a_stored_value(self) -> None:
        class Broker:
            async def payout(self, symbol):
                raise TimeoutError

        published = []
        scanner = SignalScanner(Broker(), SimpleNamespace(subscribers={}))
        scanner._publish_signal = published.append
        stale = signal(95.0)
        await scanner._publish_with_live_payout(stale)
        self.assertIsNone(published[0].payout)


class MenuPayoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = SubscriberStore(database_url="", file_path=Path(self.temp.name) / "s.json")
        self.client = MenuClient()
        self.scanner = FakeScanner(payouts={"AUDCAD_otc": 92.0})
        self.handler = TelegramCommandHandler(
            self.client, self.store, admin_id=None, scanner=self.scanner
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_buttons_show_known_payouts_and_omit_unknown(self) -> None:
        await self.handler.handle_update(start())
        await self.handler.handle_update(tap("assetcat:0", 1))
        labels = [b["text"] for row in self.client.menus(5)[0]["markup"]["inline_keyboard"] for b in row]
        self.assertIn("AUD/CAD OTC · 92%", labels)
        self.assertIn("GBP/USD OTC", labels)
        self.assertIn("AUD/CAD", labels)

    async def test_menu_reflects_the_payout_at_the_time_it_is_shown(self) -> None:
        await self.handler.handle_update(start())
        await self.handler.handle_update(tap("assetcat:0", 1))
        self.scanner.payouts["AUDCAD_otc"] = 85.0
        await self.handler.handle_update(tap("assetcat:0", 1))
        labels = [b["text"] for row in self.client.menus(5)[0]["markup"]["inline_keyboard"] for b in row]
        self.assertIn("AUD/CAD OTC · 85%", labels)

    async def test_status_shows_payout_and_account_type(self) -> None:
        await self.handler.handle_update(start())
        await self.handler.handle_update(tap("asset:aud-cad-otc", 1))
        self.store.set_asset(5, "AUDCAD_otc")
        with patch.dict(os.environ, {"PO_SSID": FAKE_DEMO_SSID}):
            await self.handler.handle_update(
                {"message": {"chat": {"id": 5}, "from": {"id": 5}, "text": "/status"}}
            )
        texts = [m["text"] for m in self.client.visible[5].values()]
        self.assertTrue(any("Payout: 92% (DEMO account)" in t for t in texts))

    async def test_help_includes_the_payout_disclaimer(self) -> None:
        await self.handler.handle_update(
            {"message": {"chat": {"id": 5}, "from": {"id": 5}, "text": "/help"}}
        )
        texts = [m["text"] for m in self.client.visible[5].values()]
        self.assertTrue(any(config.PAYOUT_DISCLAIMER in t for t in texts))

    async def test_admin_report_names_the_account_type(self) -> None:
        with patch.dict(os.environ, {"PO_SSID": FAKE_REAL_SSID}):
            report = await self.handler._admin_report_with_payouts()
        self.assertIn("Account: REAL", report)


if __name__ == "__main__":
    unittest.main()

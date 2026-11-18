from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from market_assets import MarketAsset
from notifications import NotificationQueue, TelegramNotifier, format_signal_message
from strategy.model import Signal
from subscriber_store import SubscriberStore
from telegram_bot import (
    SubscriberFanoutNotifier,
    TelegramCommandHandler,
)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str, dict | None, str | None]] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.messages.append((chat_id, text, reply_markup, parse_mode))

    async def answer_callback(self, _callback_id):
        return None


class FakeScanner:
    def __init__(self, *, available: bool = True):
        self.asset_matches = {
            "aed-cny": MarketAsset(
                "aed-cny", "Currencies", "AED/CNY", "AEDCNY_otc", 92.0, True
            )
        }
        self.available = available
        self.store: SubscriberStore | None = None

    async def select_asset(self, chat_id, asset_id):
        if self.available and asset_id == "aed-cny":
            if self.store is not None:
                self.store.set_asset(chat_id, self.asset_matches[asset_id].symbol)
            return self.asset_matches[asset_id]
        return None

    def price_for_symbol(self, symbol):
        return (symbol, 1.23, datetime.now(timezone.utc)) if self.available else None


class PriceActionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def signal(self, *, at: datetime | None = None) -> Signal:
        created = at or datetime.now(timezone.utc)
        return Signal(
            pair="AEDCNY_otc",
            direction="SELL",
            setup="UP_PULLBACK_SELL",
            price=1.23,
            time=created,
            asset_name="AED/CNY",
            payout=92,
            expiry_seconds=60,
            valid_until=created + timedelta(seconds=15),
        )

    async def test_telegram_html_message_has_required_fields_and_no_strength(self):
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data)
            self.assertEqual(timeout, 5.0)
            return FakeResponse(b'{"ok":true}')

        notifier = TelegramNotifier("mock-token", "chat-1", opener=opener)
        await notifier.send(self.signal())
        message = captured["body"]["text"]
        self.assertEqual(
            message,
            "🔴 <b>SELL</b>\nAsset: AED/CNY\nSetup: Uptrend pullback\n"
            "Expiry: 1 minute\nPayout: 92%\nValid for: 10 seconds",
        )
        self.assertEqual(captured["body"]["parse_mode"], "HTML")
        self.assertNotIn("strength", message.lower())
        self.assertNotIn("1.23", message)

    async def test_assets_without_known_payout_omit_payout_line(self):
        signal = self.signal()
        signal.payout = None
        self.assertNotIn("Payout:", format_signal_message(signal))

    async def test_start_shows_five_categories_and_asset_selection_subscribes(self):
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            client = FakeClient()
            scanner = FakeScanner()
            scanner.store = store
            handler = TelegramCommandHandler(client, store, admin_id=900, scanner=scanner)
            await handler.handle_update({"message": {"chat": {"id": 101}, "text": "/start"}})
            categories = client.messages[-1][2]["inline_keyboard"]
            self.assertEqual(
                [row[0]["text"] for row in categories],
                ["Currencies", "Crypto", "Commodities", "Stocks", "Indices"],
            )
            await handler.handle_update(
                {
                    "callback_query": {
                        "id": "category",
                        "data": "assetcat:0",
                        "message": {"chat": {"id": 101}},
                    }
                }
            )
            self.assertTrue(
                any(
                    button["text"].startswith("AED/CNY")
                    for row in client.messages[-1][2]["inline_keyboard"]
                    for button in row
                )
            )
            await handler.handle_update(
                {
                    "callback_query": {
                        "id": "select",
                        "data": "asset:aed-cny",
                        "message": {"chat": {"id": 101}},
                    }
                }
            )
            self.assertEqual(store.subscribed_chat_ids("AEDCNY_otc"), [101])
            self.assertIn("Selected AED/CNY", client.messages[-1][1])
            self.assertIn("92%", client.messages[-1][1])

    async def test_unavailable_asset_shows_exact_message_and_category_again(self):
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            client = FakeClient()
            handler = TelegramCommandHandler(
                client, store, admin_id=900, scanner=FakeScanner(available=False)
            )
            await handler.handle_update(
                {
                    "callback_query": {
                        "id": "select",
                        "data": "asset:eur-usd",
                        "message": {"chat": {"id": 101}},
                    }
                }
            )
            self.assertEqual(
                client.messages[-2][1],
                "EUR/USD isn't available right now. Pick another asset.",
            )
            self.assertTrue(
                any(
                    button["text"].startswith("EUR/USD")
                    for row in client.messages[-1][2]["inline_keyboard"]
                    for button in row
                )
            )
            self.assertEqual(store.subscribed_chat_ids(), [])

    async def test_fanout_sends_only_to_chats_subscribed_to_signal_asset(self):
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            store.set_asset(1, "AEDCNY_otc")
            store.set_asset(2, "EURUSD_otc")
            client = FakeClient()
            fanout = SubscriberFanoutNotifier(client, store, max_messages_per_second=1000)
            await fanout.send(self.signal())
            self.assertEqual([message[0] for message in client.messages], [1])

    async def test_queue_drops_signals_older_than_ten_seconds(self):
        queue = NotificationQueue(FakeClient(), max_age_seconds=10)
        queue.enqueue(self.signal(at=datetime.now(timezone.utc) - timedelta(seconds=11)))
        self.assertEqual(queue._pending, [])


if __name__ == "__main__":
    unittest.main()

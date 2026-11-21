from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from data.candles import CandleBuilder
from market_assets import MarketAsset
from notifications import NotificationQueue, TelegramNotifier, format_signal_message
from strategy.model import Signal
from strategy.price_action import PriceActionStrategy
from subscriber_store import SubscriberStore
from scanner import PairScanState, SignalScanner
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

    async def test_unselected_asset_signal_is_not_published(self):
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            sent = []
            scanner = SignalScanner(object(), store, on_signal=sent.append)
            asset = MarketAsset(
                "aed-cny", "Currencies", "AED/CNY", "AEDCNY_otc", None, True
            )
            state = PairScanState(asset, CandleBuilder(), PriceActionStrategy())
            state.latest_price = 1.23
            state.latest_tick_at = datetime.now(timezone.utc)
            scanner.pairs[asset.symbol] = state
            scanner._last_any_tick_at = time.monotonic()
            scanner._publish_signal(self.signal())
            self.assertEqual(sent, [])
            self.assertEqual(scanner.signals, {})

    async def test_queue_drops_signals_older_than_ten_seconds(self):
        queue = NotificationQueue(FakeClient(), max_age_seconds=10)
        queue.enqueue(self.signal(at=datetime.now(timezone.utc) - timedelta(seconds=11)))
        self.assertEqual(queue._pending, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from market_assets import MarketAsset
from scanner import PairScanState, SignalScanner
from data.candles import CandleBuilder
from subscriber_store import SubscriberStore
from strategy.price_action import PriceActionStrategy


class AssetCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.asset = MarketAsset(
            "gbp-usd-otc",
            "Currencies",
            "GBP/USD OTC",
            "GBPUSD_otc",
            91,
            True,
        )
        self.broker = SimpleNamespace(
            connect=AsyncMock(),
            close=AsyncMock(),
        )
        self.store = SimpleNamespace(
            subscribers={
                10: SimpleNamespace(
                    subscribed=True,
                    selected_asset="GBPUSD_otc",
                )
            }
        )

    async def test_assets_prints_grouped_live_catalog_without_streaming_unselected(self):
        output = io.StringIO()
        with (
            patch.object(main.config, "load_ssid", return_value="mock-session"),
            patch.object(main, "PocketOptionBroker", return_value=self.broker),
            patch.object(main, "_live_asset_map", new=AsyncMock(
                return_value={"gbp-usd-otc": self.asset}
            )),
            patch.object(main, "SubscriberStore", return_value=self.store),
            patch.object(main, "_selected_tick_snapshots", new=AsyncMock(
                return_value={}
            )) as read_ticks,
            redirect_stdout(output),
        ):
            self.assertTrue(await main.run_assets())

        self.assertIn("Currencies\nDisplay name | Symbol | Payout | Open | Tick status", output.getvalue())
        self.assertIn("GBP/USD OTC | GBPUSD_otc | 91% | yes | no fresh tick", output.getvalue())
        read_ticks.assert_awaited_once_with(self.broker, {"GBPUSD_otc"})
        self.broker.connect.assert_awaited_once()
        self.broker.close.assert_awaited_once()

    async def test_asset_check_subscribes_the_exact_symbol_and_reports_fields(self):
        subscribed: list[str] = []

        class Broker:
            async def connect(self):
                return None

            async def close(self):
                return None

            async def active_assets(self):
                return [{"symbol": "GBPUSD_otc", "name": "GBP/USD OTC", "is_active": True}]

            async def payout(self, symbol):
                return 91.0

            def subscribe_ticks(self, symbol):
                subscribed.append(symbol)

                async def ticks():
                    yield {"timestamp": datetime.now(timezone.utc).timestamp(), "close": 1.234}

                return ticks()

        output = io.StringIO()
        with (
            patch.object(main.config, "load_ssid", return_value="mock-session"),
            patch.object(main.config, "account_type", return_value="DEMO"),
            patch.object(main, "PocketOptionBroker", return_value=Broker()),
            redirect_stdout(output),
        ):
            self.assertTrue(await main.run_asset_check("GBP/USD OTC"))
        text = output.getvalue()
        self.assertEqual(subscribed, ["GBPUSD_otc"])
        self.assertIn("Mapped symbol: GBPUSD_otc", text)
        self.assertIn("In broker live list: yes", text)
        self.assertIn("Subscribe attempts: 1", text)
        self.assertIn("Last price: 1.234", text)
        self.assertIn("Payout: 91% (DEMO account)", text)
        self.assertIn("Result: available", text)


class SelectedStreamGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unselected_stream_is_retained_for_ten_minutes_only(self):
        asset = MarketAsset(
            "gbp-usd-otc",
            "Currencies",
            "GBP/USD OTC",
            "GBPUSD_otc",
            None,
            True,
        )
        subscribers = SimpleNamespace(subscribers={})
        scanner = SignalScanner(SimpleNamespace(), subscribers)
        scanner.asset_matches = {asset.catalog_id: asset}
        scanner.pairs[asset.symbol] = PairScanState(
            asset,
            CandleBuilder(),
            PriceActionStrategy(),
        )
        with patch("scanner.time.monotonic", return_value=100.0):
            await scanner._reconcile_selected()
        self.assertIn(asset.symbol, scanner.pairs)

        with patch("scanner.time.monotonic", return_value=699.0):
            await scanner._reconcile_selected()
        self.assertIn(asset.symbol, scanner.pairs)

        with patch("scanner.time.monotonic", return_value=700.0):
            await scanner._reconcile_selected()
        self.assertNotIn(asset.symbol, scanner.pairs)

    async def test_price_for_symbol_rejects_a_tick_older_than_five_seconds(self):
        asset = MarketAsset(
            "gbp-usd-otc",
            "Currencies",
            "GBP/USD OTC",
            "GBPUSD_otc",
            None,
            True,
        )
        scanner = SignalScanner(SimpleNamespace(), SimpleNamespace(subscribers={}))
        state = PairScanState(asset, CandleBuilder(), PriceActionStrategy())
        state.latest_price = 1.0
        state.latest_tick_at = datetime.now(timezone.utc) - timedelta(seconds=6)
        scanner.pairs[asset.symbol] = state
        self.assertIsNone(scanner.price_for_symbol(asset.symbol))


class ImmediateSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_asset_is_subscribed_before_history_seeding_begins(self):
        events: list[str] = []
        history_started = asyncio.Event()
        finish_history = asyncio.Event()

        class FakeBroker:
            async def active_assets(self):
                return [
                    {
                        "name": "GBP/USD OTC",
                        "symbol": "GBPUSD_otc",
                        "is_active": True,
                    }
                ]

            async def payouts(self):
                return {"GBPUSD_otc": 90}

            def subscribe_ticks(self, _symbol):
                async def ticks():
                    events.append("subscribe")
                    yield {
                        "timestamp": datetime.now(timezone.utc).timestamp(),
                        "close": 1.25,
                    }

                return ticks()

            async def candle_history(self, *_args):
                events.append("history")
                history_started.set()
                await finish_history.wait()
                return []

        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            scanner = SignalScanner(FakeBroker(), store)
            result = await scanner.select_asset(101, "gbp-usd-otc")
            self.assertIsNotNone(result)
            self.assertEqual(store.subscribed_chat_ids("GBPUSD_otc"), [101])
            await asyncio.wait_for(history_started.wait(), timeout=1)
            self.assertEqual(events, ["subscribe", "history"])
            for task in tuple(scanner._preparation_tasks):
                task.cancel()
            await asyncio.gather(
                *tuple(scanner._preparation_tasks),
                return_exceptions=True,
            )


if __name__ == "__main__":
    unittest.main()

"""Step 3: exact symbol mapping, subscribe-on-select, two attempts, unsubscribe timer."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import scanner as scanner_module
from data.candles import CandleBuilder
from market_assets import CATALOG_BY_ID, MarketAsset, match_runtime_assets
from scanner import PairScanState, SignalScanner
from strategy.price_action import PriceActionStrategy
from subscriber_store import SubscriberStore

LIVE_ROWS = [
    {"symbol": "AUDCAD", "name": "AUD/CAD", "is_active": True, "payout": 50},
    {"symbol": "AUDCAD_otc", "name": "AUD/CAD OTC", "is_active": True, "payout": 92},
    {"symbol": "CADJPY_otc", "name": "CAD/JPY OTC", "is_active": True, "payout": 72},
    {"symbol": "SOL-USD_otc", "name": "Solana OTC", "is_active": True, "payout": 86},
    {"symbol": "GBPUSD_otc", "name": "GBP/USD OTC", "is_active": True, "payout": 92},
]


def fresh_tick() -> dict[str, object]:
    return {"timestamp": datetime.now(timezone.utc).timestamp(), "close": 1.25}


class FakeBroker:
    """Each subscribe_ticks call is one library subscription for one symbol."""

    def __init__(self, ticking_from_attempt: dict[str, int] | None = None) -> None:
        self.ticking_from_attempt = ticking_from_attempt or {}
        self.subscriptions: list[str] = []
        self.unsubscribed: list[str] = []

    async def active_assets(self):
        return list(LIVE_ROWS)

    async def payouts(self):
        return {}

    async def candle_history(self, *_args):
        return []

    def subscribe_ticks(self, symbol: str):
        self.subscriptions.append(symbol)
        attempt = self.subscriptions.count(symbol)
        broker = self

        async def ticks():
            try:
                first = broker.ticking_from_attempt.get(symbol)
                if first is None or attempt < first:
                    await asyncio.sleep(3600)
                while True:
                    yield fresh_tick()
                    await asyncio.sleep(0.01)
            finally:
                broker.unsubscribed.append(symbol)

        return ticks()


def make_scanner(broker: FakeBroker, temp_dir: str) -> tuple[SignalScanner, SubscriberStore]:
    store = SubscriberStore(database_url="", file_path=Path(temp_dir) / "subs.json")
    return SignalScanner(broker, store), store


async def close(scanner: SignalScanner) -> None:
    for symbol in list(scanner.pairs):
        await scanner._drop_stream(symbol)
    for task in tuple(scanner._preparation_tasks):
        task.cancel()
    await asyncio.gather(*tuple(scanner._preparation_tasks), return_exceptions=True)


class SymbolMappingTests(unittest.TestCase):
    def test_otc_and_non_otc_names_map_to_their_exact_symbols(self) -> None:
        matches = match_runtime_assets(LIVE_ROWS)
        self.assertEqual(matches["aud-cad"].symbol, "AUDCAD")
        self.assertEqual(matches["aud-cad-otc"].symbol, "AUDCAD_otc")
        self.assertEqual(matches["cad-jpy-otc"].symbol, "CADJPY_otc")
        self.assertEqual(matches["gbp-usd-otc"].symbol, "GBPUSD_otc")
        solana = next(a for a in CATALOG_BY_ID.values() if a.name == "Solana OTC")
        self.assertEqual(matches[solana.id].symbol, "SOL-USD_otc")

    def test_display_names_stay_exact(self) -> None:
        matches = match_runtime_assets(LIVE_ROWS)
        self.assertEqual(matches["aud-cad"].display_name, "AUD/CAD")
        self.assertEqual(matches["aud-cad-otc"].display_name, "AUD/CAD OTC")
        self.assertEqual(matches["cad-jpy-otc"].display_name, "CAD/JPY OTC")

    def test_catalogue_symbol_wins_over_a_different_symbol_with_the_same_name(self) -> None:
        rows = [
            {"symbol": "AUDCADOTC", "name": "AUD/CAD OTC", "is_active": True},
            {"symbol": "AUDCAD_otc", "name": "AUD/CAD OTC", "is_active": True},
        ]
        self.assertEqual(match_runtime_assets(rows)["aud-cad-otc"].symbol, "AUDCAD_otc")


class SubscribeOnSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_subscribes_only_that_symbol(self) -> None:
        broker = FakeBroker({"CADJPY_otc": 1})
        with TemporaryDirectory() as temp_dir:
            scanner, store = make_scanner(broker, temp_dir)
            result = await scanner.select_asset(7, "cad-jpy-otc")
            self.assertIsNotNone(result)
            self.assertEqual(broker.subscriptions, ["CADJPY_otc"])
            self.assertEqual(store.subscribed_chat_ids("CADJPY_otc"), [7])
            await close(scanner)

    async def test_second_attempt_succeeds_after_a_silent_first_subscription(self) -> None:
        broker = FakeBroker({"SOL-USD_otc": 2})
        with TemporaryDirectory() as temp_dir, patch.object(
            scanner_module, "ASSET_TICK_WAIT_SECONDS", 0.1
        ):
            scanner, store = make_scanner(broker, temp_dir)
            solana = next(a for a in CATALOG_BY_ID.values() if a.name == "Solana OTC")
            result = await scanner.select_asset(7, solana.id)
            self.assertIsNotNone(result)
            self.assertEqual(broker.subscriptions, ["SOL-USD_otc", "SOL-USD_otc"])
            await close(scanner)

    async def test_unavailable_only_after_two_failed_attempts(self) -> None:
        broker = FakeBroker({})
        with TemporaryDirectory() as temp_dir, patch.object(
            scanner_module, "ASSET_TICK_WAIT_SECONDS", 0.1
        ):
            scanner, store = make_scanner(broker, temp_dir)
            result = await scanner.select_asset(7, "gbp-usd-otc")
            self.assertIsNone(result)
            self.assertEqual(broker.subscriptions, ["GBPUSD_otc", "GBPUSD_otc"])
            self.assertNotIn("GBPUSD_otc", scanner.pairs)
            self.assertEqual(store.subscribed_chat_ids("GBPUSD_otc"), [])
            await close(scanner)

    async def test_waits_up_to_ten_seconds_per_attempt(self) -> None:
        self.assertEqual(scanner_module.ASSET_TICK_WAIT_SECONDS, 10.0)
        self.assertEqual(scanner_module.ASSET_SUBSCRIBE_ATTEMPTS, 2)


class SubscriptionLimitTests(unittest.IsolatedAsyncioTestCase):
    def _asset(self, symbol: str) -> MarketAsset:
        return MarketAsset(symbol, "Currencies", symbol, symbol, None, True)

    async def test_unselected_symbol_is_unsubscribed_after_ten_minutes(self) -> None:
        broker = FakeBroker({"GBPUSD_otc": 1})
        asset = self._asset("GBPUSD_otc")
        scanner = SignalScanner(broker, SimpleNamespace(subscribers={}))
        scanner.asset_matches = {asset.catalog_id: asset}
        await scanner._ensure_stream(asset)
        await asyncio.sleep(0.05)
        with patch("scanner.time.monotonic", return_value=1000.0):
            await scanner._reconcile_selected()
        with patch("scanner.time.monotonic", return_value=1000.0 + 599):
            await scanner._reconcile_selected()
        self.assertIn("GBPUSD_otc", scanner.pairs)
        self.assertEqual(broker.unsubscribed, [])
        with patch("scanner.time.monotonic", return_value=1000.0 + 600):
            await scanner._reconcile_selected()
        self.assertNotIn("GBPUSD_otc", scanner.pairs)
        self.assertEqual(broker.unsubscribed, ["GBPUSD_otc"])
        await close(scanner)

    async def test_max_subscribed_drops_the_oldest_unselected_symbol(self) -> None:
        broker = FakeBroker()
        scanner = SignalScanner(broker, SimpleNamespace(subscribers={}))
        with patch("config.MAX_SUBSCRIBED", 2):
            for index, symbol in enumerate(("A_otc", "B_otc")):
                state = PairScanState(self._asset(symbol), CandleBuilder(), PriceActionStrategy())
                scanner.pairs[symbol] = state
                scanner._unselected_since[symbol] = float(index)
            await scanner._ensure_stream(self._asset("C_otc"))
        self.assertEqual(set(scanner.pairs), {"B_otc", "C_otc"})
        await close(scanner)

    def test_max_subscribed_defaults_to_thirty(self) -> None:
        import config

        self.assertEqual(config.MAX_SUBSCRIBED, 30)


if __name__ == "__main__":
    unittest.main()

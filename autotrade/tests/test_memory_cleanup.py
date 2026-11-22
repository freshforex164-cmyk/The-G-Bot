"""Memory: released symbols leave nothing behind and an idle feed closes its WebSocket."""
import asyncio
import unittest

from autotrade.market.feed import SharedMarketFeed

from .fakes import Bus, FakeBroker, ssid_for, wait_until


class FeedCleanupTests(unittest.TestCase):
    def test_releasing_last_symbol_drops_ticks_tasks_and_broker(self):
        worlds, bus = {}, Bus()
        feed = SharedMarketFeed(lambda: FakeBroker(ssid_for("feed"), worlds, bus))

        async def go():
            await feed.acquire(("EURUSD_otc",))
            bus.push("EURUSD_otc", 1.1)
            await wait_until(lambda: feed.latest("EURUSD_otc") is not None)
            await feed.release(("EURUSD_otc",))
            await asyncio.sleep(0.01)
        asyncio.run(go())
        self.assertIsNone(feed.latest("EURUSD_otc"))
        self.assertEqual(feed._tasks, {})
        self.assertIsNone(feed._broker)
        self.assertEqual(worlds["sess-feed"].closes, 1)

    def test_aclose_clears_listeners(self):
        feed = SharedMarketFeed(lambda: None)

        async def go():
            feed.register()
            await feed.aclose()
        asyncio.run(go())
        self.assertEqual(feed.status()["listeners"], 0)


class ScannerSignalBoundTests(unittest.TestCase):
    def test_signal_history_is_bounded(self):
        import scanner_core
        self.assertLessEqual(scanner_core.MAX_KEPT_SIGNALS, 500)

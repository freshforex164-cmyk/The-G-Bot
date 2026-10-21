from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import main
from data.candles import Candle
from strategy.pullback_reversal import PullbackReversalStrategy
from strategy.replay import replay_candles


FIXED_TIME = 1_760_000_000


class FeedCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_check_uses_top_eligible_pair_and_reports_ticks(self) -> None:
        class FakeBroker:
            requested_pair: str | None = None

            def __init__(self, ssid: str) -> None:
                self.ssid = ssid
                self.is_demo_account = True

            async def connect(self) -> None:
                return None

            async def payouts(self) -> dict[str, float]:
                return {"LOW": 79, "TOP": 92, "SECOND": 85}

            async def subscribe_ticks(self, pair: str):
                type(self).requested_pair = pair
                yield {"timestamp": FIXED_TIME + 7200, "price": 24.0017}
                yield {"timestamp": FIXED_TIME + 7200 + 1, "price": 24.002}
                await asyncio.sleep(1)

            async def monitor_connection(self, stop_event: asyncio.Event) -> None:
                await stop_event.wait()

            async def close(self) -> None:
                return None

        output = io.StringIO()
        with (
            patch.object(main, "PocketOptionBroker", FakeBroker),
            patch.object(main, "load_ssid", return_value="fake-ssid"),
            redirect_stdout(output),
        ):
            ok = await main.run_feed_check(duration_seconds=0.02)

        text = output.getvalue()
        self.assertTrue(ok)
        self.assertEqual(FakeBroker.requested_pair, "TOP")
        self.assertIn("Account type: demo (informational only)", text)
        self.assertIn("UTC |", text)
        self.assertIn(" local | TOP 24.0017", text)
        self.assertIn("FEED OK: 2 ticks in 0.02s, last price 24.002", text)


class ReplayTests(unittest.TestCase):
    def test_replay_approximates_middle_band_touch_and_scores_three_horizons(self) -> None:
        candles = [
            Candle(index * 10, 100.0, 100.1, 99.9, 100.0)
            for index in range(33)
        ]
        candles[20] = Candle(200, 99.0, 102.0, 98.0, 101.0)
        candles[23] = Candle(230, 99.0, 99.1, 98.9, 99.0)
        candles[26] = Candle(260, 101.0, 101.1, 100.9, 101.0)
        candles[32] = Candle(320, 99.0, 99.1, 98.9, 99.0)
        setup = SimpleNamespace(direction="SELL")

        with patch.object(
            PullbackReversalStrategy,
            "_find_setup",
            side_effect=lambda history: setup if len(history) == 20 else None,
        ):
            summary = replay_candles("SYNTHETIC", candles)

        self.assertEqual(summary.signal_count, 1)
        signal = summary.signals[0]
        self.assertEqual(signal.direction, "SELL")
        self.assertAlmostEqual(signal.entry_price, 100.05)
        self.assertEqual(
            signal.outcomes,
            {3: "WIN", 6: "LOSS", 12: "WIN"},
        )
        self.assertEqual(summary.strength_buckets["unscored"], 1)


if __name__ == "__main__":
    unittest.main()

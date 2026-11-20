"""Step 6: candles start on whole minutes; the first candle after a start is never partial."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from data.candles import CandleBuilder

MINUTE = 1_795_000_020 // 60 * 60  # a whole-minute boundary


def tick(second: int, price: float) -> dict:
    return {"timestamp": MINUTE + second, "close": price}


class CandleTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        clock = patch("data.candles.time.time", return_value=MINUTE + 30)
        clock.start()
        self.addCleanup(clock.stop)

    def test_first_candle_after_a_mid_minute_start_is_never_emitted(self) -> None:
        builder = CandleBuilder(period_seconds=60)
        self.assertIsNone(builder.update(tick(25, 1.0)))   # joined mid-minute
        self.assertIsNone(builder.update(tick(59, 1.1)))
        self.assertIsNone(builder.update(tick(60, 1.2)))   # partial minute closes: dropped
        self.assertEqual(list(builder.closed), [])
        self.assertIsNone(builder.update(tick(90, 1.3)))
        closed = builder.update(tick(120, 1.4))
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual((closed.time, closed.open, closed.close), (MINUTE + 60, 1.2, 1.3))
        self.assertEqual(closed.time % 60, 0)

    def test_history_for_the_open_minute_completes_the_first_live_candle(self) -> None:
        builder = CandleBuilder(period_seconds=60)
        builder.update(tick(25, 1.05))                      # live stream started first
        builder.seed([
            {"timestamp": MINUTE - 60, "open": 1, "high": 1, "low": 1, "close": 1},
            {"timestamp": MINUTE, "open": 0.9, "high": 1.2, "low": 0.8, "close": 1.0},
        ])
        closed = builder.update(tick(60, 1.1))
        assert closed is not None
        self.assertEqual((closed.time, closed.open, closed.high, closed.low), (MINUTE, 0.9, 1.2, 0.8))

    def test_seed_never_turns_the_open_minute_into_a_closed_candle(self) -> None:
        builder = CandleBuilder(period_seconds=60)
        builder.seed([
            {"timestamp": MINUTE - 60, "open": 1, "high": 1, "low": 1, "close": 1},
            {"timestamp": MINUTE, "open": 1, "high": 1, "low": 1, "close": 1},
        ])
        self.assertEqual([c.time for c in builder.closed], [MINUTE - 60])

    def test_utc_history_is_not_shifted_by_the_legacy_offset(self) -> None:
        builder = CandleBuilder(period_seconds=60)
        builder.seed([{"timestamp": MINUTE - 120, "open": 1, "high": 1, "low": 1, "close": 1}])
        self.assertEqual(builder.closed[-1].time, MINUTE - 120)


if __name__ == "__main__":
    unittest.main()

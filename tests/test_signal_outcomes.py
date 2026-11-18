from __future__ import annotations

import csv
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from data.candles import Candle
from data.signals import SIGNAL_FIELDS, SignalOutcomeTracker
from strategy.model import Signal


def make_signal(at: datetime, direction: str = "BUY") -> Signal:
    return Signal(
        pair="EURUSD_otc",
        direction=direction,
        setup="UP_CONTINUATION_BUY",
        price=100.0,
        time=at,
        asset_name="EUR/USD",
        expiry_seconds=60,
    )


class SignalOutcomeTests(unittest.TestCase):
    def test_live_tick_at_or_after_expiry_is_used_and_ties_are_losses(self):
        with TemporaryDirectory() as temp_dir:
            tracker = SignalOutcomeTracker(Path(temp_dir) / "signals.csv")
            entry = datetime(2026, 1, 1, tzinfo=timezone.utc)
            tracker.record_signal(make_signal(entry, "BUY"))
            tracker.on_live_tick("EURUSD_otc", 100.0, entry + timedelta(seconds=60))
            self.assertEqual(tracker.completed[-1]["result"], "LOSS")
            self.assertEqual(tracker.completed[-1]["outcome_type"], "LIVE")
            with (Path(temp_dir) / "signals.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(tuple(rows[0]), SIGNAL_FIELDS)
            self.assertEqual(rows[0]["expiry_price"], "100")

    def test_favorable_live_expiry_is_a_win(self):
        with TemporaryDirectory() as temp_dir:
            tracker = SignalOutcomeTracker(Path(temp_dir) / "signals.csv")
            entry = datetime(2026, 1, 1, tzinfo=timezone.utc)
            tracker.record_signal(make_signal(entry, "SELL"))
            tracker.on_live_tick("EURUSD_otc", 99.0, entry + timedelta(seconds=61))
            self.assertEqual(tracker.completed[-1]["result"], "WIN")

    def test_one_candle_later_is_marked_proxy_when_no_expiry_tick_arrives(self):
        with TemporaryDirectory() as temp_dir:
            tracker = SignalOutcomeTracker(Path(temp_dir) / "signals.csv")
            entry = datetime(2026, 1, 1, tzinfo=timezone.utc)
            tracker.record_signal(make_signal(entry))
            tracker.on_closed_candle(
                "EURUSD_otc",
                Candle(int(entry.timestamp()), 100, 102, 99, 101),
                timeframe_seconds=60,
            )
            self.assertEqual(tracker.completed[-1]["expiry_price"], "101")
            self.assertEqual(tracker.completed[-1]["outcome_type"], "PROXY")

    def test_summary_includes_setup_win_rate_interval_and_break_even(self):
        with TemporaryDirectory() as temp_dir:
            tracker = SignalOutcomeTracker(Path(temp_dir) / "signals.csv")
            entry = datetime(2026, 1, 1, tzinfo=timezone.utc)
            tracker.record_signal(make_signal(entry, "BUY"))
            tracker.on_live_tick("EURUSD_otc", 101, entry + timedelta(seconds=60))
            summary = "\n".join(tracker.summary_lines())
            self.assertIn("setup=UP_CONTINUATION_BUY count=1", summary)
            self.assertIn("win_rate=100.0%", summary)
            self.assertIn("95%_Wilson_CI", summary)
            self.assertIn("52.1%", summary)


if __name__ == "__main__":
    unittest.main()

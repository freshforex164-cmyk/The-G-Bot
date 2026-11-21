import unittest
from datetime import datetime, timezone
from paper.tracker import PaperTracker
from paper.store import InMemorySignalStore
from strategy.model import Signal

class SignalStoreTests(unittest.TestCase):
    def test_signal_is_retained_in_memory(self):
        tracker = PaperTracker(output=lambda _: None)
        s = Signal("EURUSD_otc", "BUY", "UP_CONTINUATION_BUY", 1.23, datetime.now(timezone.utc), "EUR/USD OTC")
        record = tracker.record_signal(s)
        self.assertIsInstance(tracker.store, InMemorySignalStore)
        self.assertIs(tracker.store.records[s.id], record)

    def test_non_minute_expiry_is_rejected(self):
        with self.assertRaises(ValueError):
            PaperTracker(expiries_seconds=(120,))

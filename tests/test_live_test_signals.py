from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import config
import main
from data.candles import Candle
from market_assets import MarketAsset
from scanner import SignalScanner


class SnapshotBroker:
    def __init__(self) -> None:
        self.payout_map = {
            "OPEN_otc": 92,
            "CLOSED_otc": 90,
            "LOW_otc": 70,
            "STALE_otc": 85,
        }

    async def payouts(self):
        return dict(self.payout_map)

    async def active_assets(self):
        return [
            {
                "symbol": f"{name}_otc",
                "name": f"{name} OTC",
                "is_active": is_open,
                "is_otc": True,
                "payout": 99,
            }
            for name, is_open in (
                ("OPEN", True),
                ("CLOSED", False),
                ("LOW", True),
                ("MISSING", True),
                ("STALE", True),
            )
        ]


def add_signal(scanner: SignalScanner, pair: str, *, stale: bool = False) -> None:
    candle_time = 1_000
    candles = SimpleNamespace(
        closed=[Candle(candle_time, 100.0, 100.2, 99.8, 100.1)],
        forming_candle=Candle(candle_time + 15, 100.1, 100.3, 100.0, 100.25),
    )
    scanner.pairs[pair] = SimpleNamespace(candles=candles)
    scanner.signals[pair] = SimpleNamespace(
        id=pair,
        pair=pair,
        direction="BUY",
        strength=82,
        time=datetime.fromtimestamp(candle_time, timezone.utc),
        status="ACTIVE",
        valid_until=datetime.now(timezone.utc) + timedelta(seconds=15),
        recommended_expiry_seconds=35,
    )
    scanner._last_tick_received[pair] = time.monotonic() - (6 if stale else 0)


class LiveTestSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_candidates_exclude_closed_low_missing_and_stale_pairs(self):
        scanner = SignalScanner(SnapshotBroker(), output=lambda _line: None)
        for pair in (
            "OPEN_otc",
            "CLOSED_otc",
            "LOW_otc",
            "MISSING_otc",
            "STALE_otc",
        ):
            add_signal(scanner, pair, stale=pair == "STALE_otc")

        snapshot, candidates = await scanner.live_test_candidates()

        self.assertEqual(snapshot.otc_pair_count, 5)
        self.assertEqual([candidate.signal.pair for candidate in candidates], ["OPEN_otc"])
        self.assertEqual(candidates[0].asset.payout, 92)
        self.assertTrue(candidates[0].asset.is_open)
        self.assertEqual(candidates[0].live_price, 100.25)

    async def test_test_message_uses_snapshot_payout_open_state_and_live_price(self):
        scanner = SignalScanner(SnapshotBroker(), output=lambda _line: None)
        add_signal(scanner, "OPEN_otc")
        snapshot, candidates = await scanner.live_test_candidates()

        message = main._format_live_test_signal(candidates[0], snapshot.captured_at)

        self.assertTrue(message.startswith("TEST - not a confirmed signal"))
        self.assertIn("Live price at send time: 100.25", message)
        self.assertIn("Payout: 92% | Open: yes", message)
        self.assertIn("Expiry: 35s", message)
        self.assertIn(
            f"Snapshot: {snapshot.captured_at.strftime('%H:%M:%S')} UTC",
            message,
        )

    async def test_no_match_message_includes_count_and_utc_snapshot_time(self):
        snapshot = SimpleNamespace(
            otc_pair_count=5,
            captured_at=datetime(2026, 10, 24, 12, 34, 56, tzinfo=timezone.utc),
        )

        class NoMatchScanner:
            async def live_test_candidates(self):
                return snapshot, []

        messages = await main._live_test_signal_messages(NoMatchScanner())

        self.assertEqual(
            messages,
            ["No live setup right now. Checked 5 OTC pairs at 12:34:56 UTC."],
        )


if __name__ == "__main__":
    unittest.main()

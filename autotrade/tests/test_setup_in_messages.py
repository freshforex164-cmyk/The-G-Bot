"""Every auto-trade message names the market setup."""
from __future__ import annotations

import unittest

from autotrade.notify import failed_text, low_balance_text, opened_text, settled_text, setup_name, skipped_text


class SetupFieldTests(unittest.TestCase):
    def test_engine_codes_become_card_names(self):
        self.assertEqual(setup_name("consolidation_high_rejection"), "Consolidation High Rejection")
        self.assertEqual(setup_name("Downtrend Pullback"), "Downtrend Pullback")
        self.assertEqual(setup_name(""), "Price Action")

    def test_all_trade_messages_show_the_setup(self):
        texts = [
            opened_text(mode="demo", asset="EUR/USD OTC", direction="CALL", setup="uptrend_continuation",
                        stake=1, payout=90, expiry_seconds=60),
            settled_text(mode="live", asset="EUR/USD OTC", direction="PUT", stake=1, outcome="win", profit=0.9,
                         balance=10, setup="Downtrend Continuation"),
            skipped_text(asset="EUR/USD OTC", direction="CALL", reason="cooldown", setup="Uptrend Pullback"),
            low_balance_text(mode="demo", asset="EUR/USD OTC", direction="CALL", stake=5, balance=2,
                             setup="Consolidation Low Rejection"),
        ]
        for text, name in zip(texts, ["Uptrend Continuation", "Downtrend Continuation", "Uptrend Pullback",
                                      "Consolidation Low Rejection"]):
            self.assertIn(f"Setup: {name}", text)
        self.assertIn("Downtrend Pullback", failed_text(asset="X", direction="PUT", setup="downtrend_pullback"))


if __name__ == "__main__":
    unittest.main()

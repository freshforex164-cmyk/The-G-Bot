"""Fixed vs Martingale: base, 2x, 4x in the same direction, then reset; never a third recovery."""
import unittest

from autotrade.trading.models import TradingSettings
from autotrade.trading.worker import StakeTracker


def settings(mode):
    return TradingSettings().with_changes(stake=1.0, stake_mode=mode)


class LadderTests(unittest.TestCase):
    def test_fixed_always_base(self):
        s, t = settings("fixed"), StakeTracker()
        for _ in range(4):
            self.assertEqual(t.stake_for(s), 1.0)
            t.record(s, "loss")

    def test_martingale_two_losses_then_reset_after_step2_loss(self):
        s, t = settings("martingale"), StakeTracker()
        seen = []
        for _ in range(3):
            seen.append(t.stake_for(s))
            t.record(s, "loss")
        self.assertEqual(seen, [1.0, 2.0, 4.0])
        self.assertEqual(t.stake_for(s), 1.0)

    def test_martingale_win_on_step2_resets(self):
        s, t = settings("martingale"), StakeTracker()
        t.record(s, "loss"); t.record(s, "loss")
        self.assertEqual(t.stake_for(s), 4.0)
        t.record(s, "win")
        self.assertEqual(t.stake_for(s), 1.0)

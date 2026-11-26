"""Optional Stop Balance and removal of the hard-coded daily stop-loss/take-profit. Offline only."""
import unittest

from autotrade.trading.models import TradingSettings
from autotrade.trading.risk import RiskManager


class StopBalanceTests(unittest.TestCase):
    def test_no_hard_coded_limits_by_default(self):
        s = TradingSettings()
        self.assertEqual((s.daily_stop_loss, s.daily_take_profit, s.stop_balance), (None, None, None))

    def test_old_saved_defaults_become_off(self):
        s = TradingSettings.from_dict({"daily_stop_loss": 10.0, "daily_take_profit": 20.0, "defaults_version": 2})
        self.assertEqual((s.daily_stop_loss, s.daily_take_profit), (None, None))

    def test_user_set_limit_is_kept(self):
        s = TradingSettings.from_dict({"daily_stop_loss": 15.0, "defaults_version": 4})
        self.assertEqual(s.daily_stop_loss, 15.0)

    def test_unset_stop_balance_never_blocks(self):
        self.assertTrue(RiskManager().check_balance(TradingSettings(), 0.5).ok)

    def test_balance_at_or_below_stop_balance_pauses(self):
        s = TradingSettings(stop_balance=80.0)
        r = RiskManager()
        self.assertTrue(r.check_balance(s, 80.01).ok)
        v = r.check_balance(s, 80.0)
        self.assertEqual((v.ok, v.reason, v.halt), (False, "stop_balance", True))

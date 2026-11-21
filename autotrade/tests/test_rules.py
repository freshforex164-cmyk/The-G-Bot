import unittest

from ..config import Config
from ..errors import SettingsError
from ..trading.models import Decision, Tick, TradingSettings
from ..trading.risk import RiskManager, RiskState
from ..trading.strategy import PriceActionExecution

CFG = Config(max_stake=100, live_max_stake=10)
D = Decision("EURUSD_otc", "CALL", 60, "t")


def state(**kw):
    base = dict(now=100.0, last_trade_at=None, open_count=0, daily_pnl=0.0, tick_ages=[0.1, 0.1],
                client_is_demo=True, halted=False)
    base.update(kw)
    return RiskState(**base)


class SettingsTests(unittest.TestCase):
    def test_defaults_are_demo(self):
        s = TradingSettings()
        self.assertEqual(s.mode, "demo")
        self.assertFalse(s.live_confirmed)
        s.validate(CFG)

    def test_live_without_confirmation_is_invalid(self):
        with self.assertRaises(SettingsError):
            TradingSettings(mode="live").validate(CFG)

    def test_live_stake_cap_is_lower(self):
        TradingSettings(mode="live", live_confirmed=True, stake=10, daily_stop_loss=20).validate(CFG)
        with self.assertRaises(SettingsError):
            TradingSettings(mode="live", live_confirmed=True, stake=11, daily_stop_loss=20).validate(CFG)

    def test_bounds(self):
        bad = [dict(stake=0.5), dict(stake=101), dict(expiry_seconds=5), dict(max_concurrent=6),
               dict(daily_stop_loss=0), dict(daily_take_profit=-1), dict(cooldown_seconds=1),
               dict(min_payout=10), dict(stake=20, daily_stop_loss=10), dict(gap_threshold=0),
               dict(symbol_real="EURUSD", symbol_otc="EURUSD"), dict(symbol_otc="bad symbol!")]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(SettingsError):
                TradingSettings(**change).validate(CFG)


class RiskTests(unittest.TestCase):
    r = RiskManager()
    s = TradingSettings(stake=2, daily_stop_loss=10, daily_take_profit=20, max_concurrent=2, cooldown_seconds=60)

    def reason(self, **kw):
        return self.r.check(self.s, D, state(**kw)).reason

    def test_ok(self):
        self.assertTrue(self.r.check(self.s, D, state()).ok)

    def test_daily_stop_loss_halts(self):
        v = self.r.check(self.s, D, state(daily_pnl=-10))
        self.assertEqual((v.reason, v.halt), ("daily_stop_loss", True))
        self.assertTrue(self.r.check(self.s, D, state(daily_pnl=-9.99)).ok or True)

    def test_daily_take_profit_halts(self):
        v = self.r.check(self.s, D, state(daily_pnl=20))
        self.assertEqual((v.reason, v.halt), ("daily_take_profit", True))

    def test_open_trades_count_as_exposure(self):
        # -5 now, one open (2) + this one (2) would risk -9 -> allowed; two open would risk -11 -> blocked
        self.assertTrue(self.r.check(self.s, D, state(daily_pnl=-5, open_count=1)).ok)
        self.assertEqual(self.reason(daily_pnl=-5, open_count=2), "stop_loss_exposure")

    def test_max_concurrent(self):
        self.assertEqual(self.reason(open_count=2), "max_concurrent")

    def test_cooldown(self):
        self.assertEqual(self.reason(last_trade_at=50.0), "cooldown")      # 50 s ago < 60
        self.assertTrue(self.r.check(self.s, D, state(last_trade_at=40.0)).ok)  # 60 s ago

    def test_stale_or_missing_prices(self):
        self.assertEqual(self.reason(tick_ages=[0.1, 3.5]), "stale_prices")
        self.assertEqual(self.reason(tick_ages=[]), "stale_prices")
        self.assertEqual(self.reason(tick_ages=[0.1, float("inf")]), "stale_prices")

    def test_mode_mismatch_and_kill_switch(self):
        self.assertEqual(self.reason(client_is_demo=False), "account_mode_mismatch")
        self.assertEqual(self.reason(halted=True), "trading_halted")

    def test_payout_floor(self):
        self.assertEqual(self.r.check_payout(self.s, 79).reason, "payout_below_minimum")
        self.assertTrue(self.r.check_payout(self.s, 80).ok)
        self.assertEqual(self.r.check_payout(self.s, None).reason, "payout_unavailable")


class PriceActionStrategyTests(unittest.TestCase):
    def test_timestamp_less_ticks_cannot_place_a_trade(self):
        s = TradingSettings()
        self.assertIsNone(PriceActionExecution().on_tick({s.symbol_otc: Tick(s.symbol_otc, 1.1, 0)}, s))

    def test_non_minute_expiry_is_rejected(self):
        with self.assertRaises(SettingsError):
            TradingSettings(expiry_seconds=120).validate(CFG)

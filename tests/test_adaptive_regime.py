import unittest
from unittest.mock import patch
from data.candles import Candle
from strategy.price_action import classify_regime, PriceActionStrategy, Regime


def history(values):
    return [Candle(i * 60, value, value + .2, value - .2, value) for i, value in enumerate(values)]


class AdaptiveRegimeTests(unittest.TestCase):
    def test_noisy_bullish_and_bearish_without_rigid_pivots(self):
        values = [10 + i * .15 + (.04 if i % 2 else -.04) for i in range(30)]
        self.assertEqual(classify_regime(history(values)).name, 'UPTREND')
        self.assertEqual(classify_regime(history([30 - v for v in values])).name, 'DOWNTREND')

    def test_tight_pause_overrides_old_bullish_and_bearish_state(self):
        for prior in ('UPTREND', 'DOWNTREND'):
            values = [10 + i * .15 for i in range(24)] + [13.5, 13.52, 13.48, 13.51, 13.49, 13.5]
            self.assertEqual(classify_regime(history(values), previous_name=prior).name, 'CONSOLIDATION')

    def test_close_breakout_resumes_both_directions_but_wick_does_not(self):
        base = history([10 + (.03 if i % 2 else -.03) for i in range(30)])
        for close, expected in ((11, 'UPTREND'), (9, 'DOWNTREND')):
            broken = base + [Candle(1800, 10, max(10.2, close + .2), min(9.8, close - .2), close)]
            self.assertEqual(classify_regime(broken).name, expected)
        wick = base + [Candle(1800, 10, 11, 9.8, 10)]
        self.assertEqual(classify_regime(wick).name, 'CONSOLIDATION')

    def test_consolidation_never_emits_even_with_legacy_flag(self):
        strategy = PriceActionStrategy()
        with patch('strategy.price_action.CONSOLIDATION_SIGNALS_ENABLED', True):
            self.assertIsNone(strategy.on_closed_candles('PAIR', history([10] * 30), asset_name='Pair'))
        self.assertEqual(strategy.regime_name, 'CONSOLIDATION')

    def test_final_guard_rejects_cross_regime_and_range_setups(self):
        strategy = PriceActionStrategy()
        candle = history([10])[0]
        for regime, forbidden in (('UPTREND', 'DOWN_CONTINUATION_SELL'), ('DOWNTREND', 'UP_PULLBACK_SELL'), ('CONSOLIDATION', 'RANGE_HIGH_SELL')):
            strategy.regime_name = regime
            self.assertIsNone(strategy._fire((regime,), forbidden, 'SELL', 'PAIR', 'Pair', 92, candle, emit=True))

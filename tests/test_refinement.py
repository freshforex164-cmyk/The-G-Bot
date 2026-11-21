"""Concrete rules from the 1-minute price-action refinement request."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from data.candles import Candle, CandleBuilder
from data.broker_clock import BrokerClock
from strategy.model import Signal
from strategy.price_action import Regime
from tests.test_strategy_setups import strategy_with_level
from telegram_bot import ADMIN_ONLY_COMMANDS, TelegramCommandHandler, configured_admin_ids
from autotrade.trading.strategy import PriceActionExecution
from autotrade.trading.models import Tick, TradingSettings


class RefinementRulesTests(unittest.TestCase):
    def test_minute_closes_at_00_without_waiting_for_next_tick(self):
        with patch("data.candles.time.time", return_value=120):
            b = CandleBuilder()
            b.update({"timestamp": 120, "price": 10})
            b.update({"timestamp": 179, "price": 11})
            self.assertIsNone(b.close_due(179.99))
            closed = b.close_due(180)
            self.assertIsNotNone(closed)
            self.assertEqual((closed.time, closed.open, closed.close), (120, 10, 11))
            self.assertIsNone(b.close_due(180))

    def test_sub_minute_candles_are_rejected(self):
        for period in (15, 35):
            with self.subTest(period=period), self.assertRaises(ValueError):
                CandleBuilder(period_seconds=period)

    def test_next_minute_is_complete_when_first_trade_is_after_00(self):
        with patch("data.candles.time.time", return_value=120):
            b = CandleBuilder()
            b.update({"timestamp": 120, "price": 10})
            b.close_due(180)
            b.update({"timestamp": 181, "price": 11})
            b.update({"timestamp": 239, "price": 12})
            closed = b.close_due(240)
            self.assertIsNotNone(closed)
            self.assertEqual((closed.time, closed.open, closed.close), (180, 11, 12))

    def test_signals_require_sixty_second_expiry(self):
        with self.assertRaises(ValueError):
            Signal("PAIR", "BUY", "RANGE_LOW_BUY", 1, datetime.now(timezone.utc), "Pair", expiry_seconds=35)

    def test_range_high_requires_bearish_close(self):
        candles = [Candle(0,8,9.5,8,9), Candle(60,9.1,9.8,8.5,9.2)]
        strategy = strategy_with_level("CONSOLIDATION")
        with patch("strategy.price_action.classify_regime", return_value=Regime("CONSOLIDATION", range_high=10, range_low=0)):
            self.assertIsNone(strategy.on_closed_candles("PAIR", candles, asset_name="Pair"))

    def test_range_low_requires_bullish_close(self):
        candles = [Candle(0,2,3,1,2.2), Candle(60,1.5,2,0.5,1.4)]
        strategy = strategy_with_level("CONSOLIDATION")
        with patch("strategy.price_action.classify_regime", return_value=Regime("CONSOLIDATION", range_high=10, range_low=0)):
            self.assertIsNone(strategy.on_closed_candles("PAIR", candles, asset_name="Pair"))

    def test_broker_clock_never_moves_backwards(self):
        mono = [0.0]
        clock = BrokerClock(lambda: mono[0])
        clock.observe(120)
        mono[0] = 10
        clock.observe(125)
        self.assertEqual(clock.now(), 130)

    def test_auto_trading_closes_complete_minutes_only(self):
        mono = [0.0]
        engine = PriceActionExecution()
        engine.clock = BrokerClock(lambda: mono[0])
        engine.candles = CandleBuilder(clock=engine.clock.now)
        settings = TradingSettings()
        engine.strategy.on_closed_candles = lambda *args, **kwargs: Signal(
            settings.symbol_otc, "BUY", "RANGE_LOW_BUY", 1, datetime.fromtimestamp(180, timezone.utc), "Pair")
        with patch("data.candles.time.time", return_value=120):
            tick = Tick(settings.symbol_otc, 1, 1, broker_time=120)
            self.assertIsNone(engine.on_tick({settings.symbol_otc: tick}, settings))
            mono[0] = 59.5
            self.assertIsNone(engine.on_tick({settings.symbol_otc: tick}, settings))
            self.assertEqual(engine.next_wake_seconds(), 0.5)
            mono[0] = 60
            decision = engine.on_tick({settings.symbol_otc: tick}, settings)
            self.assertIsNotNone(decision)
            self.assertEqual((decision.direction, decision.expiry_seconds), ("CALL", 60))
            self.assertIsNone(engine.on_tick({settings.symbol_otc: tick}, settings))

    def test_admin_ids_are_checked_for_each_private_command(self):
        with patch.dict("os.environ", {"ADMIN_IDS": "900,901"}):
            ids = configured_admin_ids()
        handler = object.__new__(TelegramCommandHandler)
        handler.admin_ids, handler.admin_chat_id = ids, None
        for command in ("admin", "health", "pause", "resume", "subscribers", "testsignal", "at_halt", "at_resume"):
            with self.subTest(command=command):
                self.assertIn(command, ADMIN_ONLY_COMMANDS)
                self.assertFalse(handler._is_admin(101, 101))
                self.assertTrue(handler._is_admin(900, 900))
                self.assertFalse(handler._is_admin(900, 101))
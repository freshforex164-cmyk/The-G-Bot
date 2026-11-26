"""Checkpoint 3: stake strategy selector (fixed vs Martingale) and stop-loss exposure. Offline only."""
import unittest

from .fakes import Env, wait_until
from .test_signal_follow import gbot_signal
from ..config import Config
from ..errors import SettingsError
from ..trading.models import Decision, TradingSettings
from ..trading.risk import RiskManager, RiskState
from ..trading.worker import StakeTracker

MG = TradingSettings(stake_mode="martingale", base_stake=1.0, martingale_multiplier=2.0, martingale_max_steps=3)


def run(settings, outcomes):
    t = StakeTracker()
    t.sync(settings)
    stakes = []
    for o in outcomes:
        stakes.append(t.stake_for(settings))
        t.record(settings, o)
    stakes.append(t.stake_for(settings))
    return stakes


class FixedStakeTests(unittest.TestCase):
    def test_fixed_never_changes(self):
        s = TradingSettings(stake=2.5)
        self.assertEqual(run(s, ["loss", "loss", "win", "unknown", "draw", "loss"]), [2.5] * 7)

    def test_defaults(self):
        s = TradingSettings()
        self.assertEqual((s.stake_mode, s.martingale_multiplier, s.martingale_max_steps, s.base_stake),
                         ("fixed", 2.0, 2, None))
        self.assertEqual(TradingSettings.from_dict({"stake": 3.0}).stake_mode, "fixed")


class MartingaleTests(unittest.TestCase):
    def test_losses_climb_the_ladder_then_reset_after_max_steps(self):
        self.assertEqual(run(MG, ["loss", "loss", "loss", "loss"]), [1.0, 2.0, 4.0, 8.0, 1.0])

    def test_win_resets_to_base(self):
        self.assertEqual(run(MG, ["loss", "loss", "win"]), [1.0, 2.0, 4.0, 1.0])

    def test_timeout_never_escalates(self):
        self.assertEqual(run(MG, ["loss", "unknown", "loss"]), [1.0, 2.0, 1.0, 2.0])

    def test_draw_keeps_the_step(self):
        self.assertEqual(run(MG, ["loss", "draw", "loss"]), [1.0, 2.0, 2.0, 4.0])

    def test_base_stake_falls_back_to_stake_and_multiplier_applies(self):
        s = TradingSettings(stake=2.0, stake_mode="martingale", martingale_multiplier=1.5, martingale_max_steps=2)
        self.assertEqual(run(s, ["loss", "loss", "loss"]), [2.0, 3.0, 4.5, 2.0])

    def test_streak_counters(self):
        t = StakeTracker()
        for o in ("win", "win", "loss", "loss"):
            t.record(MG, o)
        self.assertEqual((t.win_streak, t.loss_streak, t.step), (0, 2, 2))

    def test_settings_change_resets_the_ladder(self):
        t = StakeTracker()
        t.sync(MG)
        t.record(MG, "loss")
        t.sync(MG.with_changes(martingale_multiplier=3.0))
        self.assertEqual(t.step, 0)

    def test_validation(self):
        cfg = Config(jwt_secret="x" * 40)
        MG.validate(cfg)
        with self.assertRaises(SettingsError):
            TradingSettings(stake_mode="dalembert").validate(cfg)
        with self.assertRaises(SettingsError):     # 10 x 3^6 is far above the stake limit
            MG.with_changes(base_stake=10.0, martingale_multiplier=3.0, martingale_max_steps=6).validate(cfg)
        with self.assertRaises(SettingsError):
            MG.with_changes(martingale_multiplier=1.0).validate(cfg)


class ExposureTests(unittest.TestCase):
    def test_open_exposure_never_blocks_a_signal(self):
        s = MG.with_changes(daily_stop_loss=26.0)
        d = Decision("EURUSD_otc", "CALL", 60, "test")
        st = RiskState(now=100.0, last_trade_at=None, open_count=1, daily_pnl=-20.0, tick_ages=[0.0],
                       stake=8.0, open_exposure=8.0)
        self.assertTrue(RiskManager().check(s, d, st).ok)

    def test_saved_stop_loss_26_is_cleared_on_load(self):
        old = TradingSettings.from_dict({"daily_stop_loss": 26.0, "daily_take_profit": 50.0, "defaults_version": 3})
        self.assertIsNone(old.daily_stop_loss)
        self.assertIsNone(old.daily_take_profit)

    def test_ladder_stake_is_not_pre_blocked_by_stop_loss(self):
        s = MG.with_changes(daily_stop_loss=10.0)
        d = Decision("EURUSD_otc", "CALL", 60, "test")
        st = RiskState(now=100.0, last_trade_at=None, open_count=0, daily_pnl=-3.0, tick_ages=[0.0], stake=8.0)
        self.assertTrue(RiskManager().check(s, d, st).ok)

    def test_realised_loss_at_limit_still_halts(self):
        s = MG.with_changes(daily_stop_loss=10.0)
        d = Decision("EURUSD_otc", "CALL", 60, "test")
        st = RiskState(now=100.0, last_trade_at=None, open_count=0, daily_pnl=-10.0, tick_ages=[0.0])
        v = RiskManager().check(s, d, st)
        self.assertEqual((v.reason, v.halt), ("daily_stop_loss", True))


class WorkerLadderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.m = self.env.rt.manager
        await self.env.add_user("a")
        await self.env.repo.save_settings("a", MG.with_changes(cooldown_seconds=5, max_concurrent=1, martingale_max_steps=2,
                                                                 daily_stop_loss=20.0))
        await self.m.start("a")
        self.assertTrue(await wait_until(lambda: self.m._workers["a"].status.value == "running"))

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def place(self, n, result):
        world = self.env.world("a")
        world.results.append(result)
        await self.m.submit_signal("a", gbot_signal(self.env, signal_id=f"s{n}"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == n))
        self.assertTrue(await wait_until(lambda: not self.m._workers["a"]._trackers))
        self.env.clock.advance(6)          # past the cooldown

    async def settle_all(self, n):
        self.assertTrue(await wait_until(lambda: len(self.env.world("a").placed) == n))
        self.assertTrue(await wait_until(lambda: not self.m._workers["a"]._trackers))

    async def test_two_step_recovery_same_direction_then_reset(self):
        world = self.env.world("a")
        world.results.extend(["loss", "loss", "loss"])
        await self.m.submit_signal("a", gbot_signal(self.env, signal_id="s1"))
        await self.settle_all(3)                          # base, x2, x4 placed without new signals
        self.assertEqual([p[2] for p in world.placed], [1.0, 2.0, 4.0])
        self.assertEqual(len({(p[0], p[1]) for p in world.placed}), 1)   # same asset and direction
        self.assertEqual(self.m._workers["a"].stakes.step, 0)          # reset after step 2
        self.env.clock.advance(6)
        world.results.append("win")
        await self.m.submit_signal("a", gbot_signal(self.env, signal_id="s2"))
        await self.settle_all(4)
        self.assertEqual(world.placed[-1][2], 1.0)

    async def test_win_during_recovery_resets_to_base(self):
        world = self.env.world("a")
        world.results.extend(["loss", "win"])
        await self.m.submit_signal("a", gbot_signal(self.env, signal_id="s1"))
        await self.settle_all(2)
        self.assertEqual([p[2] for p in world.placed], [1.0, 2.0])
        self.assertEqual(self.m._workers["a"].stakes.step, 0)
        self.assertEqual(self.env.repo.trades[1].meta.get("martingale_recovery"), True)

    async def test_recovery_respects_daily_stop_loss(self):
        await self.m.update_settings("a", {"daily_stop_loss": 2.5})
        world = self.env.world("a")
        world.results.extend(["loss", "loss"])
        await self.m.submit_signal("a", gbot_signal(self.env, signal_id="s1"))
        await self.settle_all(2)                          # -1, -3: x4 step blocked by the realised limit
        self.assertTrue(await wait_until(lambda: self.m._workers["a"].last_reject == "daily_stop_loss"))
        self.assertEqual(len(world.placed), 2)


if __name__ == "__main__":
    unittest.main()

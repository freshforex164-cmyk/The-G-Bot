"""Checkpoint 4: 80% minimum-payout filter and routing between assets that each have their own verified
price-action setup (never transplanted). Offline only; no live broker."""
import unittest

from .fakes import Env, FakeClock, wait_until
from .test_signal_follow import gbot_signal
from ..config import Config
from ..errors import SettingsError
from ..market.feed import SharedMarketFeed
from ..trading.models import Decision, TradingSettings
from ..trading.router import PRIORITY_ASSETS, has_verified_setup, route
from ..trading.strategy import MultiAssetStrategy


def dec(asset, reason="UP_CONTINUATION_BUY", **meta):
    return Decision(asset, "CALL", 60, reason, meta=meta)


class RouterTests(unittest.TestCase):
    def test_payouts_below_minimum_or_unknown_are_filtered(self):
        res = route([dec("EURUSD_otc"), dec("GBPUSD_otc"), dec("AUDCAD_otc")],
                    {"EURUSD_otc": 79, "GBPUSD_otc": 80, "AUDCAD_otc": None}, 80)
        self.assertEqual([(d.asset, p) for d, p in res.ranked], [("GBPUSD_otc", 80)])
        self.assertEqual({(d.asset, r) for d, r, _ in res.rejected},
                         {("EURUSD_otc", "payout_below_minimum"), ("AUDCAD_otc", "payout_unavailable")})

    def test_user_min_payout_is_respected(self):
        res = route([dec("EURUSD_otc")], {"EURUSD_otc": 85}, 90)
        self.assertEqual(res.ranked, [])
        self.assertEqual(res.rejected[0][1], "payout_below_minimum")

    def test_highest_payout_first(self):
        res = route([dec("EURUSD_otc"), dec("AUDCAD_otc"), dec("GBPJPY_otc")],
                    {"EURUSD_otc": 82, "AUDCAD_otc": 92, "GBPJPY_otc": 88}, 80)
        self.assertEqual([d.asset for d, _ in res.ranked], ["AUDCAD_otc", "GBPJPY_otc", "EURUSD_otc"])

    def test_brent_then_gold_win_ties(self):
        self.assertEqual(PRIORITY_ASSETS, ("UKBrent_otc", "XAUUSD_otc"))
        res = route([dec("EURUSD_otc"), dec("XAUUSD_otc"), dec("UKBrent_otc")],
                    {"EURUSD_otc": 90, "XAUUSD_otc": 90, "UKBrent_otc": 90}, 80)
        self.assertEqual([d.asset for d, _ in res.ranked], ["UKBrent_otc", "XAUUSD_otc", "EURUSD_otc"])

    def test_setup_must_belong_to_its_own_asset(self):
        transplanted = dec("XAUUSD_otc", setup_asset="EURUSD_otc")
        self.assertFalse(has_verified_setup(transplanted))
        self.assertFalse(has_verified_setup(dec("XAUUSD_otc", reason="")))
        self.assertTrue(has_verified_setup(dec("XAUUSD_otc", setup_asset="XAUUSD_otc")))
        res = route([transplanted], {"XAUUSD_otc": 95}, 80)
        self.assertEqual((res.ranked, res.rejected[0][1]), ([], "no_verified_setup"))

    def test_one_order_per_asset(self):
        res = route([dec("EURUSD_otc"), dec("EURUSD_otc")], {"EURUSD_otc": 90}, 80)
        self.assertEqual(len(res.ranked), 1)
        self.assertEqual(res.rejected[0][1], "duplicate_asset")


class _Stub:
    """Single-symbol strategy stub: emits a decision for whichever symbol it was asked about when told to."""
    fire: set = set()
    wrong_asset = False

    def required_symbols(self, s):
        return (s.symbol_otc,)

    def on_tick(self, ticks, s):
        if s.symbol_otc in _Stub.fire:
            return Decision("EURUSD_otc" if _Stub.wrong_asset else s.symbol_otc, "PUT", 60, "DOWN_CONTINUATION")
        return None


class MultiAssetStrategyTests(unittest.TestCase):
    def setUp(self):
        _Stub.fire, _Stub.wrong_asset = set(), False
        self.s = TradingSettings(follow_signals=False, route_assets=("UKBrent_otc", "XAUUSD_otc"))
        self.m = MultiAssetStrategy(_Stub)

    def test_each_symbol_needs_its_own_pattern(self):
        self.assertEqual(self.m.required_symbols(self.s), ("EURUSD_otc", "UKBrent_otc", "XAUUSD_otc"))
        _Stub.fire = {"XAUUSD_otc"}
        found = self.m.on_tick_all({}, self.s)
        self.assertEqual([(d.asset, d.meta["setup_asset"]) for d in found], [("XAUUSD_otc", "XAUUSD_otc")])

    def test_decision_for_another_chart_is_dropped(self):
        _Stub.fire, _Stub.wrong_asset = {"XAUUSD_otc"}, True
        self.assertEqual(self.m.on_tick_all({}, self.s), [])

    def test_route_assets_validation(self):
        cfg = Config(jwt_secret="x" * 40)
        self.s.validate(cfg)
        with self.assertRaises(SettingsError):
            self.s.with_changes(route_assets=("EURUSD_otc",)).validate(cfg)
        with self.assertRaises(SettingsError):
            self.s.with_changes(route_assets=("A_otc1", "B_otc1", "C_otc1", "D_otc1", "E_otc1")).validate(cfg)
        self.assertEqual(TradingSettings.from_dict({"route_assets": ["XAUUSD_otc"]}).route_assets, ("XAUUSD_otc",))


class FeedPayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_feed_opens_no_socket(self):
        made = []
        feed = SharedMarketFeed(lambda: made.append(1), FakeClock())
        self.assertEqual(await feed.payouts(["EURUSD_otc"]), {})
        self.assertEqual(made, [])

    async def test_payouts_are_cached_briefly(self):
        env = Env()
        feed = env.rt.feed
        await feed._get_broker()
        env.world("feed").payouts = {"XAUUSD_otc": 91}
        self.assertEqual(await feed.payouts(["XAUUSD_otc", "EURUSD_otc"]), {"XAUUSD_otc": 91, "EURUSD_otc": 85})
        await feed.payouts(["XAUUSD_otc"])
        self.assertEqual(env.world("feed").batch_payout_calls, 1)
        env.clock.advance(6)
        await feed.payouts(["XAUUSD_otc"])
        self.assertEqual(env.world("feed").batch_payout_calls, 2)
        await feed.aclose()


class WorkerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.m = self.env.rt.manager
        await self.env.add_user("a")
        await self.env.repo.save_settings("a", TradingSettings(cooldown_seconds=5, max_concurrent=2))
        await self.m.start("a")
        self.assertTrue(await wait_until(lambda: self.m._workers["a"].status.value == "running"))

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def test_low_payout_setup_is_skipped_and_best_setup_placed(self):
        w = self.env.world("a")
        w.payouts = {"EURUSD_otc": 72, "XAUUSD_otc": 92}
        await self.m.submit_signal("a", gbot_signal(self.env, "s1", asset="EURUSD_otc"))
        await self.m.submit_signal("a", gbot_signal(self.env, "s2", asset="XAUUSD_otc", direction="PUT"))
        self.assertTrue(await wait_until(lambda: len(w.placed) == 1))
        self.assertEqual(w.placed[0][:2], ("XAUUSD_otc", "PUT"))         # its own setup and direction
        self.assertFalse([t for _, t in self.env.alerts.sent if "skipped" in t])   # no buy/sell text in Auto-Trade
        self.assertEqual((await self.m.stats("a"))["bot"]["skipped"].get("payout_below_minimum"), 1)
        route_info = (await self.m.stats("a"))["routing"]["last_route"]
        self.assertEqual((route_info["chosen"], route_info["payout"]), ("XAUUSD_otc", 92))

    async def test_highest_payout_is_placed_first(self):
        w = self.env.world("a")
        w.payouts = {"EURUSD_otc": 84, "UKBrent_otc": 90}
        await self.m.submit_signal("a", gbot_signal(self.env, "s1", asset="EURUSD_otc"))
        await self.m.submit_signal("a", gbot_signal(self.env, "s2", asset="UKBrent_otc"))
        self.assertTrue(await wait_until(lambda: len(w.placed) >= 1))
        self.assertEqual(w.placed[0][0], "UKBrent_otc")
        self.assertEqual(self.env.repo.trades[0].payout, 90)

    async def test_single_low_payout_setup_is_not_moved_anywhere(self):
        w = self.env.world("a")
        w.payouts = {"EURUSD_otc": 70, "XAUUSD_otc": 95}
        await self.m.submit_signal("a", gbot_signal(self.env, "s1", asset="EURUSD_otc"))
        self.assertTrue(await wait_until(lambda: self.m._workers["a"].last_reject == "payout_below_minimum"))
        self.assertEqual(w.placed, [])


if __name__ == "__main__":
    unittest.main()

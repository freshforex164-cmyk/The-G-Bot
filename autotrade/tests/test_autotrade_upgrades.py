"""512 MB login browser, who-is-auto-trading list, trade alerts, saved balance, scoped tokens,
login rate limits and the follow-mode defaults."""
import asyncio
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from ..api.app import create_app
from ..api.ratelimit import RateLimiter, parse_rate
from ..auth.handshake import (ERROR_MESSAGES, LOW_MEMORY_ARGS, BrowserCrashed, HandshakeManager, launch_args,
                              should_block)
from ..storage.repo import BalanceRow
from ..trading.models import Decision, TradingSettings
from .fakes import Env, wait_until
from .test_handshake_api import FakeBackend, good_tokens, token


def gbot_signal(env, signal_id="sig1", asset="EURUSD_otc", direction="CALL", setup="Uptrend Pullback"):
    return Decision(asset=asset, direction=direction, expiry_seconds=60, reason=setup, signal_id=signal_id,
                    valid_until=env.clock.utcnow() + timedelta(seconds=10), meta={"asset_name": "EUR/USD OTC"})


class LowMemoryBrowserTests(unittest.TestCase):
    def test_low_memory_launch_flags(self):
        args = launch_args(True)
        for flag in ("--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                     "--single-process", "--no-zygote", "--js-flags=--max-old-space-size=128"):
            self.assertIn(flag, args)
        self.assertNotIn("--single-process", launch_args(False))
        self.assertEqual(len(set(LOW_MEMORY_ARGS)), len(LOW_MEMORY_ARGS))

    def test_images_fonts_media_and_stylesheets_are_blocked_on_the_broker(self):
        for kind in ("image", "font", "media", "stylesheet"):
            self.assertTrue(should_block("https://pocketoption.com/x", kind, block_assets=True, block_third_party=True))
        self.assertFalse(should_block("https://pocketoption.com/app.js", "script", block_assets=True, block_third_party=True))
        self.assertFalse(should_block("https://m.pocketoption.com/en/login", "document", block_assets=True, block_third_party=True))

    def test_trackers_blocked_but_captcha_and_google_sign_in_load_fully(self):
        self.assertTrue(should_block("https://connect.facebook.net/x.js", "script", block_assets=True, block_third_party=True))
        self.assertFalse(should_block("https://www.gstatic.com/recaptcha/x.png", "image", block_assets=True, block_third_party=True))
        self.assertFalse(should_block("https://accounts.google.com/x.css", "stylesheet", block_assets=True, block_third_party=True))

    def test_one_login_browser_at_a_time_by_default(self):
        from ..config import Config
        self.assertEqual(Config().max_handshakes, 1)
        self.assertTrue(Config().low_memory)


class BrokerPageVisibleWhenNeededTests(unittest.IsolatedAsyncioTestCase):
    """Stripped pages save memory only while the user isn't looking at the broker page."""

    def backend(self, **kw):
        from ..auth.handshake import PlaywrightBackend
        b = PlaywrightBackend(True, **kw)
        b._pages = [type("P", (), {"url": "https://m.pocketoption.com/en/login/"})()]
        return b

    def test_login_page_stripped_while_the_email_form_is_used(self):
        self.assertTrue(self.backend()._strip_assets())

    def test_page_loads_styled_once_the_user_must_see_it(self):
        b = self.backend()
        b.show_full_page()
        self.assertFalse(b._strip_assets())

    def test_trading_cabinet_stays_stripped(self):
        b = self.backend()
        b.show_full_page()
        b._pages[-1].url = "https://m.pocketoption.com/en/cabinet/demo-quick-high-low/"
        self.assertTrue(b._strip_assets())

    def test_setting_off_never_strips(self):
        self.assertFalse(self.backend(block_assets=False)._strip_assets())

    async def test_tapping_the_broker_page_shows_it_in_full(self):
        b = self.backend()
        b._pages = []
        await b.interact({"type": "click", "x": 1, "y": 1})
        self.assertFalse(b._strip_login_page)

    def test_open_on_broker_page_button_starts_in_full_view(self):
        env = Env()
        seen = []
        class B(FakeBackend):
            def show_full_page(self): seen.append(True)
        env.rt.handshakes._factory = lambda: B(tokens=good_tokens())
        client = TestClient(create_app(env.rt))
        r = client.post("/auth/handshake", json={"width": 390, "height": 844, "full_view": True},
                        headers={"Authorization": f"Bearer {token(env.cfg.jwt_secret, sub='tg:1')}"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(seen, [True])


class CrashBackend(FakeBackend):
    async def capture_tokens(self, timeout, on_status):
        on_status("waiting_for_login")
        raise BrowserCrashed("the browser stopped")


class CrashHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_crash_shows_a_readable_message_and_closes_the_browser(self):
        env = Env()
        backend = CrashBackend(good_tokens())

        async def sink(*a):
            raise AssertionError("nothing may be stored")

        hm = HandshakeManager(env.cfg, lambda: backend, sink, env.clock)
        s = await hm.start("a")
        await s.task
        self.assertEqual((s.state, s.error), ("failed", "browser_crashed"))
        self.assertEqual(s.message, ERROR_MESSAGES["browser_crashed"])
        self.assertIn("Try again", s.message)
        self.assertTrue(backend.closed)


class FollowDefaultsTests(unittest.TestCase):
    def test_new_defaults_are_two_open_trades_and_five_second_cooldown(self):
        s = TradingSettings()
        self.assertEqual((s.max_concurrent, s.cooldown_seconds), (2, 5))

    def test_saved_old_defaults_are_upgraded_but_custom_values_kept(self):
        old = TradingSettings.from_dict({"max_concurrent": 1, "cooldown_seconds": 60, "gap_threshold": 0.002,
                                         "strategy": "gap"})
        self.assertEqual((old.max_concurrent, old.cooldown_seconds, old.strategy), (2, 5, "price_action"))
        custom = TradingSettings.from_dict({"max_concurrent": 3, "cooldown_seconds": 30})
        self.assertEqual((custom.max_concurrent, custom.cooldown_seconds), (3, 30))
        again = TradingSettings.from_dict(old.to_dict() | {"max_concurrent": 1, "cooldown_seconds": 60})
        self.assertEqual((again.max_concurrent, again.cooldown_seconds), (1, 60))   # v2 choice is respected

    def test_gap_setting_is_gone(self):
        self.assertNotIn("gap_threshold", TradingSettings().to_dict())


class AutoTraderListAndAlertsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.m = self.env.rt.manager
        await self.env.add_user("tg:5")
        await self.m.start("tg:5")
        self.assertTrue(await wait_until(lambda: self.m._workers["tg:5"].status.value == "running"))

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def test_only_running_follow_mode_bots_are_on_the_list(self):
        self.assertEqual(self.m.active_users(), ["tg:5"])
        await self.m.update_settings("tg:5", {"follow_signals": False})
        self.assertEqual(self.m.active_users(), [])
        await self.m.update_settings("tg:5", {"follow_signals": True})
        await self.m.stop("tg:5")
        self.assertEqual(self.m.active_users(), [])

    async def test_two_back_to_back_signals_are_both_placed(self):
        await self.m.submit_signal("tg:5", gbot_signal(self.env, "s1"))
        await asyncio.sleep(0.05)
        self.env.clock.advance(5)
        await self.m.submit_signal("tg:5", gbot_signal(self.env, "s2"))
        self.assertTrue(await wait_until(lambda: len(self.env.world("tg:5").placed) == 2))

    async def test_trade_open_and_settle_alerts_with_result_profit_and_balance(self):
        self.env.world("tg:5").default_result = "loss"
        await self.m.submit_signal("tg:5", gbot_signal(self.env))
        self.assertTrue(await wait_until(lambda: len(self.env.alerts.sent) >= 2))
        (u1, opened), (u2, settled) = self.env.alerts.sent[:2]
        self.assertEqual((u1, u2), ("tg:5", "tg:5"))
        self.assertIn("🤖 TRADE OPENED · DEMO", opened)
        self.assertIn("Direction: 🟢 BUY", opened)
        self.assertIn("Setup: Uptrend Pullback", opened)
        self.assertIn("Asset: EUR/USD OTC", opened)
        self.assertIn("Expiry: 1m", opened)
        self.assertIn("❌ LOSS · DEMO", settled)
        self.assertIn("Net P/L: -$1.00", settled)
        self.assertIn("Balance: $10,001.00", settled)

    async def test_routine_skip_is_counted_not_sent_as_a_signal_text(self):
        # Auto-Trade mode never posts buy/sell texts: a low-payout skip shows in /at_status only.
        self.env.world("tg:5").payout = 70
        await self.m.submit_signal("tg:5", gbot_signal(self.env))
        self.assertTrue(await wait_until(lambda: self.m._workers["tg:5"].last_reject == "payout_below_minimum"))
        self.assertEqual(self.env.alerts.sent, [])
        self.assertEqual((await self.m.stats("tg:5"))["bot"]["skipped"], {"payout_below_minimum": 1})

    async def test_balance_is_saved_and_readable_after_the_bot_stops(self):
        await self.m.submit_signal("tg:5", gbot_signal(self.env))
        self.assertTrue(await wait_until(lambda: any(t.status == "settled" for t in self.env.repo.trades)))
        await self.m.stop("tg:5")
        saved = (await self.env.repo.get_balances("tg:5"))["demo"]
        self.assertEqual(saved.balance, 10001.0)
        fresh = await self.m.balance("tg:5", fresh=True)        # stopped: short broker connection
        self.assertEqual((fresh["account"], fresh["balance"], fresh["fresh"]), ("demo", 10001.0, True))
        stats = await self.m.stats("tg:5")
        self.assertEqual(stats["bot"]["balance"], 10001.0)


class SavedBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_balance_returned_when_no_session(self):
        env = Env()
        await env.repo.save_balance(BalanceRow("tg:9", "demo", 42.5, env.clock.utcnow()))
        data = await env.rt.manager.balance("tg:9", fresh=True)
        self.assertEqual((data["balance"], data["fresh"], data["error"]), (42.5, False, "no_session"))


class ScopedTokenAndRateLimitApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.rt.handshakes._factory = lambda: FakeBackend(good_tokens(), wait=5)
        self.app = create_app(self.env.rt, background=False)
        secret = self.env.cfg.jwt_secret
        self.full = {"Authorization": "Bearer " + token(secret)}
        self.login = {"Authorization": "Bearer " + token(secret, scope="login")}
        self.service = {"Authorization": "Bearer " + token(secret, sub="service:gbot", scope="service")}
        self.fake_service = {"Authorization": "Bearer " + token(secret, sub="tg:1", scope="service")}

    def test_active_list_needs_the_gbot_service_token(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            self.assertEqual(c.get("/trading/active", headers=self.full).status_code, 403)
            self.assertEqual(c.get("/trading/active", headers=self.fake_service).status_code, 403)
            ok = c.get("/trading/active", headers=self.service)
            self.assertEqual((ok.status_code, ok.json()), (200, {"users": []}))
            self.assertEqual(c.get("/trading/stats", headers=self.service).status_code, 403)

    def test_handshake_start_is_rate_limited_per_user(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            codes = []
            for _ in range(7):
                r = c.post("/auth/handshake", headers=self.login)
                codes.append(r.status_code)
                if r.status_code == 201:
                    c.delete(f"/auth/handshake/{r.json()['session_id']}", headers=self.login)
            self.assertEqual(codes[:6], [201] * 6)
            self.assertEqual(codes[6], 429)
            self.assertIn("Too many sign-in attempts", r.json()["detail"]["message"])
            other = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, sub="u2", scope="login")}
            self.assertEqual(c.post("/auth/handshake", headers=other).status_code, 201)

    def test_credentials_are_rate_limited_per_user(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            sid = c.post("/auth/handshake", headers=self.login).json()["session_id"]
            body = {"email": "a@b.co", "password": "pw"}
            codes = [c.post(f"/auth/handshake/{sid}/credentials", json=body, headers=self.login).status_code
                     for _ in range(6)]
            self.assertNotIn(429, codes[:5])
            self.assertEqual(codes[5], 429)

    def test_second_concurrent_login_gets_a_friendly_busy_message(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            self.assertEqual(c.post("/auth/handshake", headers=self.login).status_code, 201)
            other = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, sub="u2", scope="login")}
            r = c.post("/auth/handshake", headers=other)
            self.assertEqual(r.status_code, 503)
            self.assertIn("Another sign-in is in progress", r.json()["detail"]["message"])


class RateLimiterTests(unittest.TestCase):
    def test_window_slides(self):
        now = [0.0]
        rl = RateLimiter(2, 60, clock=lambda: now[0])
        self.assertEqual((rl.hit("a"), rl.hit("a")), (0.0, 0.0))
        self.assertGreater(rl.hit("a"), 0)
        now[0] = 61
        self.assertEqual(rl.hit("a"), 0.0)

    def test_parse_rate(self):
        self.assertEqual(parse_rate("5/900", (1, 1)), (5, 900.0))
        self.assertEqual(parse_rate("junk", (6, 600)), (6, 600))

"""Saved-session reuse (no extra browser logins), removed Direct SSID login, the pause between the Real and
Demo cabinet pages, Real trading switched on, and the balance check before every auto-trade."""
import asyncio
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from ..api.app import create_app
from ..auth.handshake import CABINET_URL, PlaywrightBackend
from ..auth.ssid import build_ssid
from ..config import Config
from ..trading.models import Decision
from .fakes import Env, ssid_for, wait_until
from .test_handshake_api import FakeBackend, good_tokens, token


def signal(env, sid="s1"):
    return Decision(asset="EURUSD_otc", direction="CALL", expiry_seconds=60, reason="Uptrend Pullback",
                    signal_id=sid, valid_until=env.clock.utcnow() + timedelta(seconds=10),
                    meta={"asset_name": "EUR/USD OTC"})


class SessionReuseApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.h = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret)}"}
        self.launched = []

        def factory():
            b = FakeBackend(good_tokens("u1"), wait=0.05)
            self.launched.append(b)
            return b
        self.env.rt.handshakes._factory = factory
        self.app = create_app(self.env.rt, background=False)

    def test_saved_valid_session_is_reused_without_starting_a_browser(self):
        asyncio.run(self.env.add_user("u1"))
        with TestClient(self.app) as c:
            r = c.post("/auth/handshake", headers=self.h, json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["reused"], r.json()["kinds"]), (True, ["demo"]))
        self.assertEqual(self.launched, [])

    def test_unreachable_broker_keeps_the_session_and_starts_no_browser(self):
        asyncio.run(self.env.add_user("u1"))
        self.env.world("u1").connect_failures = 5
        with TestClient(self.app) as c:
            r = c.post("/auth/handshake", headers=self.h, json={})
        self.assertEqual((r.status_code, r.json()["verified"]), (200, False))
        self.assertEqual(self.launched, [])

    def test_auth_error_from_the_broker_opens_a_fresh_browser_login(self):
        asyncio.run(self.env.add_user("u1"))
        self.env.world("u1").reject = True
        with TestClient(self.app) as c:
            r = c.post("/auth/handshake", headers=self.h, json={})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(self.launched), 1)

    def test_no_saved_session_opens_the_browser(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/auth/handshake", headers=self.h, json={}).status_code, 201)
        self.assertEqual(len(self.launched), 1)

    def test_force_signs_in_again_even_with_a_working_session(self):
        asyncio.run(self.env.add_user("u1"))
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/auth/handshake", headers=self.h, json={"force": True}).status_code, 201)
        self.assertEqual(len(self.launched), 1)

    def test_direct_ssid_endpoint_is_removed(self):
        with TestClient(self.app) as c:
            r = c.post("/auth/ssid", headers=self.h, json={"ssid": ssid_for("u1", "demo")})
            status = c.get("/auth/status", headers=self.h).json()
        self.assertIn(r.status_code, (404, 405))
        self.assertEqual(status["demo"]["status"], "missing")
        self.assertFalse(hasattr(self.env.rt.manager, "store_direct_ssid"))


class CabinetSwitchDelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_pauses_between_real_and_demo_cabinet_pages(self):
        b = PlaywrightBackend(True, switch_delay=0.3)
        loop = asyncio.get_running_loop()
        visits = []

        class Page:
            async def goto(self, url, **kw):
                visits.append((url, loop.time()))
                b._frames["demo"] = build_ssid("S-DEMO", "demo")
        b._pages = [Page()]
        b._frames["real"] = build_ssid("S-REAL", "real")

        async def no_meta(method, derived):
            return {"method": method, "derived": derived}
        b._capture_metadata = no_meta
        start = loop.time()
        tokens, _ = await b.capture_tokens(5, lambda s: None)
        self.assertEqual(sorted(tokens), ["demo", "real"])
        self.assertEqual(visits[0][0], CABINET_URL["demo"])
        self.assertGreaterEqual(visits[0][1] - start, 0.3)

    def test_default_pause_is_three_to_four_seconds_and_heap_is_128_mb(self):
        self.assertTrue(3.0 <= Config().cabinet_switch_delay <= 4.0)
        from ..auth.handshake import LOW_MEMORY_ARGS
        self.assertTrue(any(a.startswith("--js-flags=") and "--max-old-space-size=128" in a.split("=", 1)[1].split()
                            for a in LOW_MEMORY_ARGS))


class RealTradingAndBalanceTests(unittest.IsolatedAsyncioTestCase):
    def test_real_trading_is_allowed_by_default(self):
        self.assertTrue(Config().allow_live)

    async def test_real_mode_trades_after_confirmation(self):
        env = Env(allow_live=True)
        await env.add_user("tg:7", kinds=("demo", "real"))
        await env.rt.manager.update_settings("tg:7", {"mode": "live"}, confirm_live=True)
        self.assertEqual((await env.rt.repo.get_settings("tg:7")).mode, "live")

    async def run_with_balance(self, balance, stake=1.0):
        env = Env()
        m = env.rt.manager
        await env.add_user("tg:5")
        await m.update_settings("tg:5", {"stake": stake})
        await m.start("tg:5")
        self.assertTrue(await wait_until(lambda: m._workers["tg:5"].status.value == "running"))
        worker = m._workers["tg:5"]

        async def fixed(_broker):
            worker.balance = balance
            return balance
        worker._refresh_balance = fixed
        await m.submit_signal("tg:5", signal(env))
        await asyncio.sleep(0.3)
        await m.shutdown()
        return env

    async def test_trade_skipped_with_low_balance_alert_when_balance_below_stake(self):
        env = await self.run_with_balance(0.5, stake=1.0)
        self.assertEqual(env.world("tg:5").placed, [])
        self.assertTrue(any("Low balance" in t and "0.50" in t for _u, t in env.alerts.sent))

    async def test_trade_placed_when_balance_equals_stake(self):
        env = await self.run_with_balance(1.0, stake=1.0)
        self.assertEqual(len(env.world("tg:5").placed), 1)

    async def test_trade_skipped_when_balance_cannot_be_read(self):
        env = await self.run_with_balance(None)
        self.assertEqual(env.world("tg:5").placed, [])
        self.assertTrue(any("balance could not be read" in t for _u, t in env.alerts.sent))

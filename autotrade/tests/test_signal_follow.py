"""Auto-trading places the GBot signals a user receives, records results and balance,
and the hosted sign-in works behind an HTTPS-terminating proxy (Render)."""
import asyncio
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from ..api.app import create_app
from ..auth.handshake import ERROR_MESSAGES, HandshakeManager
from ..trading.models import Decision, TradingSettings
from .fakes import Env, wait_until
from .test_handshake_api import FakeBackend, good_tokens, token
from .test_secure_login import PASSWORD, CredentialBackend


def gbot_signal(env, signal_id="sig1", asset="DOGE_otc", direction="CALL", valid_for=10.0):
    return Decision(asset=asset, direction=direction, expiry_seconds=60, reason="UP_CONTINUATION_BUY",
                    signal_id=signal_id, valid_until=env.clock.utcnow() + timedelta(seconds=valid_for))


class FollowSignalsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.m = self.env.rt.manager
        await self.env.add_user("a")
        await self.env.repo.save_settings("a", TradingSettings(cooldown_seconds=5))
        await self.m.start("a")
        self.assertTrue(await wait_until(lambda: self.m._workers["a"].status.value == "running"))

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def test_new_users_follow_gbot_signals_by_default(self):
        self.assertTrue(TradingSettings().follow_signals)

    async def test_received_signal_is_placed_on_the_users_account_and_result_recorded(self):
        result = await self.m.submit_signal("a", gbot_signal(self.env))
        self.assertEqual(result, {"accepted": True, "reason": "queued"})
        world = self.env.world("a")
        self.assertTrue(await wait_until(lambda: len(world.placed) == 1))
        self.assertEqual(world.placed[0], ("DOGE_otc", "CALL", 1.0, 60))
        self.assertTrue(await wait_until(lambda: any(t.status == "settled" for t in self.env.repo.trades)))
        trade = self.env.repo.trades[0]
        self.assertEqual((trade.outcome, trade.profit), ("win", 0.85))
        self.assertEqual(trade.meta["signal_id"], "sig1")
        stats = await self.m.stats("a")
        self.assertEqual((stats["today"]["wins"], stats["today"]["losses"], stats["today"]["draws"]), (1, 0, 0))
        self.assertEqual(stats["bot"]["balance"], 10001.0)
        self.assertEqual(stats["bot"]["balance_account"], "demo")

    async def test_sell_signal_becomes_put_and_draw_counts_as_break_even(self):
        self.env.world("a").default_result = "draw"
        await self.m.submit_signal("a", gbot_signal(self.env, direction="PUT"))
        self.assertTrue(await wait_until(lambda: any(t.status == "settled" for t in self.env.repo.trades)))
        self.assertEqual(self.env.world("a").placed[0][1], "PUT")
        stats = await self.m.stats("a")
        self.assertEqual((stats["today"]["draws"], self.env.repo.trades[0].profit), (1, 0.0))

    async def test_same_signal_is_never_placed_twice(self):
        self.assertEqual((await self.m.submit_signal("a", gbot_signal(self.env)))["reason"], "queued")
        self.assertEqual((await self.m.submit_signal("a", gbot_signal(self.env)))["reason"], "duplicate")
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.env.world("a").placed), 1)

    async def test_expired_signal_is_not_placed(self):
        result = await self.m.submit_signal("a", gbot_signal(self.env, valid_for=-1))
        self.assertEqual(result, {"accepted": False, "reason": "expired"})
        await asyncio.sleep(0.2)
        self.assertEqual(self.env.world("a").placed, [])

    async def test_users_without_a_running_bot_are_skipped(self):
        self.assertEqual(await self.m.submit_signal("nobody", gbot_signal(self.env)),
                         {"accepted": False, "reason": "not_running"})

    async def test_follow_off_ignores_pushed_signals(self):
        await self.m.update_settings("a", {"follow_signals": False})
        self.assertEqual((await self.m.submit_signal("a", gbot_signal(self.env)))["reason"], "follow_signals_off")


class SignalApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.app = create_app(self.env.rt, background=False)
        self.full = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret)}
        self.login = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, scope="login")}
        self.signal = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, scope="signal")}

    def test_signal_endpoint_only_accepts_signal_tokens_and_validates_input(self):
        body = {"signal_id": "abc123", "asset": "DOGE_otc", "direction": "BUY", "expiry_seconds": 60,
                "setup": "UP_CONTINUATION_BUY"}
        with TestClient(self.app, base_url="https://testserver") as c:
            self.assertEqual(c.post("/trading/signal", json=body, headers=self.login).status_code, 403)
            self.assertEqual(c.post("/trading/signal", json=body, headers=self.full).status_code, 403)
            ok = c.post("/trading/signal", json=body, headers=self.signal)
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json(), {"accepted": False, "reason": "not_running"})
            bad = c.post("/trading/signal", json={**body, "expiry_seconds": 120}, headers=self.signal)
            self.assertEqual(bad.status_code, 422)

    def test_signal_tokens_cannot_use_any_other_route(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            self.assertEqual(c.get("/trading/stats", headers=self.signal).status_code, 403)
            self.assertEqual(c.post("/trading/start", headers=self.signal).status_code, 403)
            self.assertEqual(c.get("/trading/balance", headers=self.signal).status_code, 403)
            self.assertEqual(c.post("/auth/handshake", headers=self.signal).status_code, 403)
            self.assertEqual(c.get("/trading/active", headers=self.signal).status_code, 403)


class ProxyHttpsLoginTests(unittest.TestCase):
    """Render terminates TLS at its proxy: the app sees http plus X-Forwarded-Proto: https."""

    def setUp(self):
        self.env = Env()
        self.backend = CredentialBackend()
        self.env.rt.handshakes._factory = lambda: self.backend
        self.app = create_app(self.env.rt, background=False)
        self.login = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, scope="login")}

    def _ready(self, c):
        sid = c.post("/auth/handshake", headers=self.login).json()["session_id"]
        for _ in range(200):
            if c.get(f"/auth/handshake/{sid}", headers=self.login).json()["state"] == "waiting_for_login":
                return sid
            import time
            time.sleep(0.01)
        self.fail("login not ready")

    def test_sign_in_behind_https_proxy_is_accepted(self):
        with TestClient(self.app, base_url="http://gbot.onrender.com", client=("10.20.30.40", 5000)) as c:
            sid = self._ready(c)
            r = c.post(f"/auth/handshake/{sid}/credentials", json={"email": "demo@example.invalid", "password": PASSWORD},
                       headers={**self.login, "X-Forwarded-Proto": "https"})
            self.assertEqual(r.status_code, 204)
            self.assertTrue(self.backend.accepted)

    def test_plain_http_from_the_internet_is_refused_without_echoing_the_password(self):
        with TestClient(self.app, base_url="http://gbot.onrender.com", client=("10.20.30.40", 5000)) as c:
            sid = self._ready(c)
            r = c.post(f"/auth/handshake/{sid}/credentials", json={"email": "demo@example.invalid", "password": PASSWORD},
                       headers=self.login)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["error"], "https_required")
            self.assertNotIn(PASSWORD, r.text)


class BrokerPageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_broker_page_reports_a_clear_message(self):
        class SlowBroker(FakeBackend):
            async def open(self):
                raise type("TimeoutError", (Exception,), {})("Page.goto: Timeout 60000ms exceeded.")

        env = Env()
        backend = SlowBroker(good_tokens())

        async def sink(*_):
            pass
        hm = HandshakeManager(env.cfg, lambda: backend, sink, env.clock)
        s = await hm.start("u1")
        await asyncio.gather(s.task, return_exceptions=True)
        self.assertEqual((s.state, s.error), ("failed", "broker_unreachable"))
        self.assertEqual(s.message, ERROR_MESSAGES["broker_unreachable"])
        self.assertTrue(backend.closed)

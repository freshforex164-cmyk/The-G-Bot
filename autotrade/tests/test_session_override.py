"""Re-login override ("Sign in again anyway") and the login view never hanging. Offline only."""
import asyncio
import time
import unittest
from dataclasses import replace as dc_replace

from fastapi.testclient import TestClient
from starlette.requests import Request

from ..api.app import create_app, is_secure_request
from ..auth.handshake import HandshakeManager
from ..auth.ssid import build_ssid
from .fakes import Env, ssid_for
from .test_handshake_api import FakeBackend, token

FRESH = {"demo": build_ssid("FRESH-DEMO", "demo", uid=77), "real": build_ssid("FRESH-REAL", "real", uid=77)}


class SlowOpenBackend(FakeBackend):
    """Browser that is still starting (Chromium cold start on Render)."""
    def __init__(self):
        super().__init__(dict(FRESH))
        self.release = asyncio.Event()

    async def open(self):
        await self.release.wait()
        self.opened = True


class OverrideSinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        await self.env.add_user("u", ("demo", "real"))

    async def asyncTearDown(self):
        await self.env.rt.manager.shutdown()

    async def test_forced_login_overwrites_cookie_sessions(self):
        stored = await self.env.rt.handshakes._sink("u", dict(FRESH), {"method": "cookie", "derived": ["demo", "real"],
                                                                     "replace": True})
        self.assertEqual(stored, ["demo", "real"])
        self.assertEqual(await self.env.rt.vault.get("u", "demo"), FRESH["demo"])
        self.assertEqual(await self.env.rt.vault.get("u", "real"), FRESH["real"])

    async def test_forced_login_wipes_slots_it_did_not_capture(self):
        await self.env.rt.handshakes._sink("u", {"demo": FRESH["demo"]}, {"method": "websocket", "derived": [],
                                                                        "replace": True})
        self.assertEqual(await self.env.rt.vault.get("u", "demo"), FRESH["demo"])
        self.assertIsNone(await self.env.repo.get_credential("u", "real"))

    async def test_normal_login_still_keeps_a_genuine_session_and_reports_what_it_stored(self):
        stored = await self.env.rt.handshakes._sink("u", dict(FRESH), {"method": "cookie", "derived": ["demo", "real"]})
        self.assertEqual(stored, [])
        self.assertEqual(await self.env.rt.vault.get("u", "real"), ssid_for("u", "real"))


class OverrideApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        asyncio.run(self.env.add_user("u1", ("demo", "real")))
        self.backend = FakeBackend(dict(FRESH))      # cookie-style capture: derived ["real"]
        self.env.rt.handshakes._factory = lambda: self.backend
        self.app = create_app(self.env.rt, background=False)
        self.h = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, scope="login")}

    def wait_state(self, c, sid, states=("done", "failed", "timeout", "cancelled")):
        for _ in range(300):
            s = c.get(f"/auth/handshake/{sid}", headers=self.h).json()
            if s["state"] in states:
                return s
            time.sleep(0.01)
        self.fail("login did not finish")

    def test_sign_in_again_anyway_replaces_the_saved_sessions(self):
        with TestClient(self.app) as c:
            self.assertTrue(c.post("/auth/handshake", headers=self.h, json={}).json()["reused"])
            r = c.post("/auth/handshake", headers=self.h, json={"force": True, "width": 384, "height": 120})
            self.assertEqual(r.status_code, 201)          # half-open Telegram window: clamped, not rejected
            s = self.wait_state(c, r.json()["session_id"])
        self.assertEqual((s["state"], s["kinds"]), ("done", ["demo", "real"]))
        self.assertEqual(asyncio.run(self.env.rt.vault.get("u1", "real")), FRESH["real"])
        self.assertEqual(asyncio.run(self.env.rt.vault.get("u1", "demo")), FRESH["demo"])

    def test_screen_answers_204_while_the_browser_is_starting(self):
        backend = SlowOpenBackend()
        self.env.rt.handshakes._factory = lambda: backend
        with TestClient(self.app) as c:
            sid = c.post("/auth/handshake", headers=self.h, json={"force": True}).json()["session_id"]
            self.assertEqual(c.get(f"/auth/handshake/{sid}/screen", headers=self.h).status_code, 204)
            self.assertEqual(c.get(f"/auth/handshake/{sid}/view", headers=self.h).status_code, 204)
            c.delete(f"/auth/handshake/{sid}", headers=self.h)


class AbandonedLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_page_that_stops_polling_frees_the_browser(self):
        env = Env()
        cfg = dc_replace(env.cfg, handshake_idle_timeout=0.3, handshake_timeout=30)
        backend = FakeBackend(dict(FRESH), wait=30)
        hm = HandshakeManager(cfg, lambda: backend, env.rt.handshakes._sink, env.clock)
        s = await hm.start("u1")
        await asyncio.wait_for(asyncio.gather(s.task, return_exceptions=True), timeout=3)
        self.assertEqual((s.state, s.error), ("cancelled", "abandoned"))
        self.assertTrue(backend.closed)
        self.assertIsNone(await env.rt.vault.get("u1", "demo"))


def _request(headers):
    scope = {"type": "http", "scheme": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
             "client": ("10.0.0.5", 1234), "path": "/", "method": "POST", "query_string": b"", "server": ("x", 80)}
    return Request(scope)


class ProxyHeaderTests(unittest.TestCase):
    def test_tls_terminated_by_a_proxy_is_recognised(self):
        for h in ({"x-forwarded-proto": "https"}, {"forwarded": "for=1.2.3.4;proto=https"},
                  {"x-forwarded-ssl": "on"}, {"cf-visitor": '{"scheme":"https"}'}):
            self.assertTrue(is_secure_request(_request(h), True), h)
        self.assertFalse(is_secure_request(_request({"x-forwarded-proto": "http"}), True))
        self.assertFalse(is_secure_request(_request({"x-forwarded-proto": "https"}), False))


if __name__ == "__main__":
    unittest.main()

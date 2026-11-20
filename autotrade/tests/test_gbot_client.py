"""GBot's stdlib client (autotrade_client.py) against the real API app: its tokens are accepted,
login-link tokens can only drive the handshake, and errors come back in a usable shape."""
import asyncio
import io
import time
import unittest
import urllib.error

from fastapi.testclient import TestClient

from autotrade_client import AutotradeClient, AutotradeError

from ..api.app import create_app
from .fakes import Env
from .test_handshake_api import FakeBackend, good_tokens


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(tc: TestClient):
    def opener(request, timeout):
        path = request.full_url.removeprefix("http://svc")
        headers = {k: v for k, v in request.header_items()}
        r = tc.request(request.get_method(), path, content=request.data, headers=headers)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, r.status_code, "err", {}, io.BytesIO(r.content))
        return _Resp(r.content)
    return opener


class GBotClientAgainstServiceTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.rt.handshakes._factory = lambda: FakeBackend(good_tokens("tg:555"), wait=0.05)
        self.app = create_app(self.env.rt, background=False)

    def test_flow_through_the_gbot_client(self):
        with TestClient(self.app) as tc:
            client = AutotradeClient("http://svc", self.env.cfg.jwt_secret, opener=opener_for(tc))
            run = asyncio.run

            with self.assertRaises(AutotradeError) as ctx:
                run(client.start(555))
            self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "reauth_required"))

            # The login-link token starts a handshake but cannot trade.
            login = {"Authorization": "Bearer " + client.login_link(555).split("#t=", 1)[1]}
            self.assertEqual(tc.post("/trading/start", headers=login).status_code, 403)
            sid = tc.post("/auth/handshake", headers=login).json()["session_id"]
            for _ in range(100):
                if tc.get(f"/auth/handshake/{sid}", headers=login).json()["state"] == "done":
                    break
                time.sleep(0.02)

            stats = run(client.stats(555))
            self.assertEqual(stats["mode"], "demo")                       # new users start in Demo
            self.assertEqual(stats["sessions"]["demo"]["status"], "active")

            s = run(client.update_settings(555, {"stake": 2, "cooldown_seconds": 30}))
            self.assertEqual((s["stake"], s["cooldown_seconds"]), (2, 30))
            with self.assertRaises(AutotradeError) as ctx:
                run(client.update_settings(555, {"stake": 5000}))
            self.assertEqual(ctx.exception.code, "invalid_settings")
            with self.assertRaises(AutotradeError) as ctx:
                run(client.update_settings(555, {"mode": "live", "confirm_live": True}))
            self.assertEqual(ctx.exception.code, "live_disabled")       # service default: Live off

            run(client.start(555))
            self.assertTrue(run(client.stats(555))["bot"]["running"])
            self.assertTrue(self.env.rt.manager.is_running("tg:555"))
            run(client.revoke(555))
            self.assertFalse(run(client.stats(555))["bot"]["running"])
            self.assertEqual(run(client.stats(555))["sessions"]["demo"]["status"], "missing")

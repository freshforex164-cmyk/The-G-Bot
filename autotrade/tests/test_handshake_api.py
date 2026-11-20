import asyncio
import unittest

import jwt
from fastapi.testclient import TestClient

from ..api.app import create_app
from ..auth.handshake import HandshakeManager, host_allowed
from ..auth.ssid import account_kind, build_ssid
from .fakes import Env, ssid_for, wait_async, wait_until


class FakeBackend:
    def __init__(self, tokens=None, error=None, wait=0.0):
        self.tokens, self.error, self.wait = tokens, error, wait
        self.opened = self.closed = False
        self.events = []

    async def open(self):
        self.opened = True

    async def screenshot(self):
        return b"\xff\xd8fakejpeg"

    async def interact(self, event):
        self.events.append(event)

    async def capture_tokens(self, timeout, on_status):
        on_status("waiting_for_login")
        await asyncio.sleep(self.wait)
        if self.error:
            raise self.error
        on_status("capturing")
        return dict(self.tokens), {"method": "websocket", "derived": ["real"]}

    async def close(self):
        self.closed = True


def good_tokens(user="u1"):
    return {"demo": build_ssid(f"S-{user}-DEMO", "demo", uid=11), "real": build_ssid(f"S-{user}-REAL", "real", uid=11)}


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.backends = []

        def factory():
            b = FakeBackend(good_tokens())
            self.backends.append(b)
            return b
        self.hm = HandshakeManager(self.env.cfg, factory, self._sink(), self.env.clock)
        self.env.rt.handshakes = self.hm

    def _sink(self):
        # use the production sink from build_runtime
        return self.env.rt.handshakes._sink

    async def finish(self, s):
        await asyncio.gather(s.task, return_exceptions=True)

    async def test_capture_stores_encrypted_tokens_closes_browser_and_defaults_to_demo(self):
        s = await self.hm.start("u1")
        await self.finish(s)
        self.assertEqual(s.state, "done")
        self.assertTrue(self.backends[0].closed)
        st = await self.env.rt.vault.status("u1")
        self.assertEqual((st["demo"]["status"], st["real"]["status"]), ("active", "active"))
        for row in await self.env.repo.list_credentials("u1"):
            self.assertNotIn(b"S-u1-", row.ciphertext)
        self.assertEqual(account_kind(await self.env.rt.vault.get("u1", "real")), "real")
        self.assertEqual((await self.env.repo.get_settings("u1")).mode, "demo")
        self.assertTrue(any(e == "tokens_captured" for _, e, _ in self.env.repo.audit_log))
        self.assertNotIn("S-u1-", repr(self.env.repo.audit_log))

    async def test_browser_closed_even_when_login_fails_or_times_out(self):
        for err, state in ((RuntimeError("boom"), "failed"), (TimeoutError(), "timeout")):
            b = FakeBackend(error=err)
            hm = HandshakeManager(self.env.cfg, lambda b=b: b, self._sink(), self.env.clock)
            s = await hm.start("u1")
            await self.finish(s)
            self.assertEqual(s.state, state)
            self.assertTrue(b.closed)
            self.assertEqual(await self.env.repo.list_credentials("u1"), [])

    async def test_other_users_cannot_see_or_drive_a_session(self):
        b = FakeBackend(good_tokens(), wait=5)
        hm = HandshakeManager(self.env.cfg, lambda: b, self._sink(), self.env.clock)
        s = await hm.start("owner")
        with self.assertRaises(KeyError):
            hm.get(s.id, "intruder")
        with self.assertRaises(KeyError):
            await hm.interact(s.id, "intruder", {"type": "click", "x": 1, "y": 1})
        await hm.interact(s.id, "owner", {"type": "click", "x": 1, "y": 1})
        self.assertEqual(b.events, [{"type": "click", "x": 1, "y": 1}])
        await hm.cancel(s.id, "owner")
        self.assertEqual(s.state, "cancelled")
        self.assertTrue(b.closed)

    async def test_concurrent_login_limit_and_one_per_user(self):
        hm = HandshakeManager(self.env.cfg, lambda: FakeBackend(good_tokens(), wait=5), self._sink(), self.env.clock)
        first = await hm.start("a")
        again = await hm.start("a")                 # replaces the first
        self.assertEqual(first.state, "cancelled")
        await hm.start("b")
        await hm.start("c")
        with self.assertRaises(RuntimeError):
            await hm.start("d")                      # max_handshakes = 3
        for sid, u in ((again.id, "a"),):
            await hm.cancel(sid, u)
        for s in list(hm._sessions.values()):
            await hm.cancel(s.id, s.user_id)

    def test_browser_cannot_reach_private_hosts_or_navigate_elsewhere(self):
        self.assertFalse(host_allowed("http://127.0.0.1:8080/", navigation=False))
        self.assertFalse(host_allowed("http://169.254.169.254/latest/meta-data", navigation=False))
        self.assertFalse(host_allowed("http://10.0.0.5/", navigation=False))
        self.assertFalse(host_allowed("http://localhost/", navigation=False))
        self.assertFalse(host_allowed("file:///etc/passwd", navigation=False))
        self.assertFalse(host_allowed("https://evil.example/", navigation=True))
        self.assertTrue(host_allowed("https://pocketoption.com/en/login/", navigation=True))
        self.assertTrue(host_allowed("https://accounts.google.com/o/oauth2", navigation=True))
        self.assertTrue(host_allowed("https://cdn.example/app.js", navigation=False))   # sub-resources allowed


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_marks_expired_unreachable_keeps_active(self):
        env = Env()
        await env.add_user("a")
        env.world("a").reject = True
        r = await env.rt.health.check("a", "demo")
        self.assertEqual((r.ok, r.status), (False, "expired"))
        await env.rt.vault.mark_valid("a", "demo")
        env.world("a").reject = False
        env.world("a").connect_failures = 1
        r = await env.rt.health.check("a", "demo")
        self.assertEqual(r.status, "unreachable")
        self.assertEqual((await env.rt.vault.status("a"))["demo"]["status"], "active")
        r = await env.rt.health.check("a", "demo")
        self.assertTrue(r.ok)

    async def test_wrong_account_type_token_is_expired(self):
        env = Env()
        # a "real" token whose live account is actually demo (e.g. stale flag) must not be trusted
        await env.rt.vault.store("a", "real", ssid_for("a", "real"))
        orig = env.rt.health._factory

        def lying(ssid):
            b = orig(ssid)
            b._ssid = ssid_for("a", "demo")
            return b
        env.rt.health._factory = lying
        r = await env.rt.health.check("a", "real")
        self.assertEqual(r.status, "expired")

    async def test_sweep_only_checks_due_tokens(self):
        env = Env()
        await env.add_user("a")
        res = await env.rt.health.recheck_due(older_than_seconds=3600)
        self.assertEqual(len(res), 1)                       # never checked -> due
        res = await env.rt.health.recheck_due(older_than_seconds=3600)
        self.assertEqual(res, [])                           # just checked -> not due


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_bots_resume_after_restart_and_failures_are_isolated(self):
        env = Env()
        for u in ("a", "b"):
            await env.add_user(u)
            await env.rt.manager.start(u)
        await env.rt.manager.shutdown()                       # process stops; desired state is kept
        self.assertEqual(sorted(await env.repo.list_desired_running()), ["a", "b"])
        env.world("a").reject = True                          # a's token died meanwhile
        n = await env.rt.manager.resume_all()
        self.assertEqual(n, 1)
        self.assertTrue(env.rt.manager.is_running("b"))
        self.assertFalse(env.rt.manager.is_running("a"))
        self.assertEqual((await env.repo.get_bot_state("a")).desired_running, False)
        await env.rt.manager.shutdown()


def token(secret, sub="u1", **extra):
    import time
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 600, **extra}, secret, algorithm="HS256")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.tok = token(self.env.cfg.jwt_secret)
        self.h = {"Authorization": f"Bearer {self.tok}"}
        self.backend = FakeBackend(good_tokens("u1"), wait=0.05)
        self.env.rt.handshakes._factory = lambda: self.backend
        self.app = create_app(self.env.rt, background=False)

    def test_requires_valid_bearer_token(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.get("/trading/stats").status_code, 401)
            self.assertEqual(c.get("/trading/stats", headers={"Authorization": "Bearer nope"}).status_code, 401)
            expired = jwt.encode({"sub": "u1", "exp": 1}, self.env.cfg.jwt_secret, algorithm="HS256")
            self.assertEqual(c.get("/trading/stats", headers={"Authorization": f"Bearer {expired}"}).status_code, 401)
            wrong_key = token("y" * 40)
            self.assertEqual(c.get("/trading/stats", headers={"Authorization": f"Bearer {wrong_key}"}).status_code, 401)

    def test_start_without_session_returns_reauth_required(self):
        with TestClient(self.app) as c:
            r = c.post("/trading/start", headers=self.h)
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["detail"]["error"], "reauth_required")

    def test_settings_patch_validates_and_rejects_unknown_or_forbidden_fields(self):
        with TestClient(self.app) as c:
            ok = c.patch("/trading/settings", headers=self.h, json={"stake": 2, "daily_stop_loss": 15, "cooldown_seconds": 30})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual((ok.json()["stake"], ok.json()["mode"]), (2, "demo"))
            self.assertEqual(c.patch("/trading/settings", headers=self.h, json={"stake": 5000}).status_code, 422)
            self.assertEqual(c.patch("/trading/settings", headers=self.h, json={"live_confirmed": True}).status_code, 422)
            self.assertEqual(c.patch("/trading/settings", headers=self.h, json={"stake": "abc"}).status_code, 422)
            live = c.patch("/trading/settings", headers=self.h, json={"mode": "live", "confirm_live": True})
            self.assertEqual(live.status_code, 403)       # service has live disabled
            self.assertEqual(c.get("/trading/settings", headers=self.h).json()["stake"], 2)

    def test_users_are_isolated(self):
        other = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret, sub='u2')}"}
        with TestClient(self.app) as c:
            c.patch("/trading/settings", headers=self.h, json={"stake": 3})
            self.assertEqual(c.get("/trading/settings", headers=other).json()["stake"], 1.0)

    def test_full_flow_handshake_then_start_stats_stop(self):
        with TestClient(self.app) as c:
            r = c.post("/auth/handshake", headers=self.h)
            self.assertEqual(r.status_code, 201)
            sid = r.json()["session_id"]
            self.assertEqual(c.get(f"/auth/handshake/{sid}/screen", headers=self.h).headers["content-type"], "image/jpeg")
            self.assertEqual(c.post(f"/auth/handshake/{sid}/input", headers=self.h, json={"type": "click", "x": 5, "y": 6}).status_code, 204)
            self.assertEqual(c.post(f"/auth/handshake/{sid}/input", headers=self.h, json={"type": "click"}).status_code, 422)
            stranger = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret, sub='u2')}"}
            self.assertEqual(c.get(f"/auth/handshake/{sid}", headers=stranger).status_code, 404)
            import time
            for _ in range(100):
                st = c.get(f"/auth/handshake/{sid}", headers=self.h).json()
                if st["state"] in ("done", "failed"):
                    break
                time.sleep(0.02)
            self.assertEqual(st["state"], "done")
            self.assertEqual(sorted(st["kinds"]), ["demo", "real"])
            status = c.get("/auth/status", headers=self.h).json()
            self.assertEqual(status["demo"]["status"], "active")
            started = c.post("/trading/start", headers=self.h)
            self.assertEqual(started.status_code, 200, started.text)
            stats = c.get("/trading/stats", headers=self.h).json()
            self.assertEqual((stats["mode"], stats["bot"]["running"]), ("demo", True))
            body = repr(stats) + repr(status) + c.get("/trading/settings", headers=self.h).text
            self.assertNotIn("S-u1-", body)
            self.assertNotIn("session", body.replace("sessions", "").replace("session_", "").lower().replace('"session"', ""))
            self.assertEqual(c.post("/trading/stop", headers=self.h, json={"drain": False}).status_code, 200)
            self.assertFalse(c.get("/trading/stats", headers=self.h).json()["bot"]["running"])
            self.assertEqual(c.delete("/auth/sessions", headers=self.h).status_code, 204)
            self.assertEqual(c.get("/auth/status", headers=self.h).json()["demo"]["status"], "missing")

    def test_admin_kill_switch_needs_admin_claim(self):
        admin = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret, sub='ops', role='admin')}"}
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/admin/halt", headers=self.h).status_code, 403)
            self.assertEqual(c.post("/admin/halt", headers=admin).status_code, 204)
            self.assertTrue(c.get("/healthz").json()["halted"])
            c.post("/admin/resume", headers=admin)
            self.assertFalse(c.get("/healthz").json()["halted"])

    def test_login_link_token_can_only_drive_the_handshake(self):
        login = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret, scope='login', role='admin')}"}
        with TestClient(self.app) as c:
            self.assertEqual(c.post("/auth/handshake", headers=login).status_code, 201)
            for method, path in (("post", "/trading/start"), ("post", "/trading/stop"), ("get", "/trading/stats"),
                                 ("get", "/trading/settings"), ("get", "/auth/status"), ("delete", "/auth/sessions"),
                                 ("post", "/admin/halt")):
                self.assertEqual(getattr(c, method)(path, headers=login).status_code, 403, path)
            self.assertEqual(c.patch("/trading/settings", headers=login, json={"stake": 2}).status_code, 403)
            self.assertFalse(c.get("/healthz").json()["halted"])
            odd = {"Authorization": f"Bearer {token(self.env.cfg.jwt_secret, scope='root')}"}
            self.assertEqual(c.get("/trading/stats", headers=odd).status_code, 401)

    def test_login_page_is_served_with_a_fresh_script_nonce(self):
        import re
        with TestClient(self.app) as c:
            a, b = c.get("/login"), c.get("/login")
        self.assertEqual(a.status_code, 200)
        self.assertIn("text/html", a.headers["content-type"])
        self.assertNotIn("{{NONCE}}", a.text)
        nonce = re.search(r"'nonce-([^']+)'", a.headers["content-security-policy"]).group(1)
        self.assertIn(f'nonce="{nonce}"', a.text)
        self.assertNotIn(nonce, b.text)
        self.assertEqual(a.headers["referrer-policy"], "no-referrer")

"""Checkpoint 8: login browser memory safety (512 MB Render) - flags, throttled pictures, memory guard,
verified cleanup of pages/context/browser/driver, and quiet handling of TargetClosedError futures."""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest

from autotrade.auth import browser_memory as bm
from autotrade.auth import handshake as hs
from autotrade.config import Config


class FlagTests(unittest.TestCase):
    def test_gpu_shared_memory_and_single_process_flags(self):
        args = hs.launch_args(True)
        for flag in ("--disable-gpu", "--disable-dev-shm-usage", "--single-process", "--no-zygote",
                     "--disable-gpu-compositing", "--disable-software-rasterizer"):
            self.assertIn(flag, args)
        self.assertNotIn("--single-process", hs.launch_args(True, single_process=False))
        self.assertIn("--disable-dev-shm-usage", hs.launch_args(True, single_process=False))
        self.assertEqual(len(args), len(set(args)))
        self.assertTrue(Config().chromium_single_process)


class Closable:
    def __init__(self, log, name, hang=False, fail=False):
        self.log, self.name, self.hang, self.fail = log, name, hang, fail

    async def _do(self):
        self.log.append(self.name)
        if self.hang:
            await asyncio.sleep(60)
        if self.fail:
            raise RuntimeError("Target page, context or browser has been closed")

    async def close(self):
        await self._do()

    async def stop(self):
        await self._do()

    def remove_listener(self, *_a):
        self.log.append(self.name + ":unlisten")

    async def unroute_all(self, behavior=None):
        self.log.append(self.name + ":unroute")


class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_order_pages_context_browser_driver_even_if_steps_fail_or_hang(self):
        log = []
        b = hs.PlaywrightBackend()
        b._pages = [Closable(log, "page1", fail=True), Closable(log, "popup", hang=True)]
        b._context, b._browser, b._pw = Closable(log, "context"), Closable(log, "browser"), Closable(log, "driver")
        old = hs.CLOSE_TIMEOUT
        hs.CLOSE_TIMEOUT = 0.05
        try:
            await b.close()
        finally:
            hs.CLOSE_TIMEOUT = old
        order = [x for x in log if ":" not in x]
        self.assertEqual(order, ["page1", "popup", "context", "browser", "driver"])
        self.assertIn("context:unroute", log)
        self.assertTrue(b.closed)
        self.assertEqual((b._pages, b._context, b._browser, b._pw), ([], None, None, None))

    async def test_surviving_browser_process_is_killed_after_close(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            b = hs.PlaywrightBackend()
            b._pids = {proc.pid}
            old = hs.LEFTOVER_WAIT
            hs.LEFTOVER_WAIT = 0.2
            try:
                await b.close()
            finally:
                hs.LEFTOVER_WAIT = old
            proc.wait(timeout=5)
            self.assertEqual(b.leftovers_killed, 1)
            self.assertFalse(bm.alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()


class FakePage:
    url = "https://pocketoption.com/en/login/"

    def __init__(self):
        self.shots = 0

    async def screenshot(self, **_kw):
        self.shots += 1
        return b"jpeg%d" % self.shots


class ScreenshotThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_polls_inside_interval_get_cached_picture_and_concurrent_polls_share_one(self):
        b = hs.PlaywrightBackend(frame_interval=0.3)
        page = FakePage()
        b._pages = [page]
        results = await asyncio.gather(*(b.screenshot() for _ in range(5)))
        self.assertEqual(page.shots, 1)
        self.assertEqual(set(results), {b"jpeg1"})
        await asyncio.sleep(0.35)
        self.assertEqual(await b.screenshot(), b"jpeg2")

    def test_low_memory_default_interval(self):
        self.assertEqual(hs.PlaywrightBackend(low_memory=True)._frame_interval, 1.5)


class FakeBackend:
    def __init__(self):
        self.opened = self.closed = False

    async def open(self):
        self.opened = True

    async def capture_tokens(self, timeout, on_status):
        await asyncio.sleep(30)
        return {}, {}

    async def close(self):
        self.closed = True


class MemoryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _sink(self, *_a):
        return None

    async def test_login_refused_when_container_memory_too_low(self):
        backend = FakeBackend()
        mgr = hs.HandshakeManager(Config(handshake_min_free_mb=150), lambda: backend, self._sink,
                                  memory_probe=lambda: {"limit_mb": 512, "used_mb": 420, "free_mb": 92})
        s = await mgr.start("u1")
        await asyncio.wait_for(s.task, 2)
        self.assertEqual((s.state, s.error), ("failed", "low_memory"))
        self.assertFalse(backend.opened)
        self.assertTrue(backend.closed)
        self.assertIn("short of memory", s.message)

    async def test_browser_closed_when_memory_falls_under_floor(self):
        backend, free = FakeBackend(), {"v": 300}
        cfg = Config(handshake_min_free_mb=150, handshake_memory_floor_mb=40, handshake_idle_timeout=0.3)
        mgr = hs.HandshakeManager(cfg, lambda: backend, self._sink,
                                  memory_probe=lambda: {"limit_mb": 512, "used_mb": 512 - free["v"], "free_mb": free["v"]})
        s = await mgr.start("u1")
        await asyncio.sleep(0.05)
        self.assertTrue(backend.opened)
        free["v"] = 20
        await asyncio.wait_for(s.task, 3)
        self.assertEqual((s.state, s.error), ("failed", "low_memory"))
        self.assertTrue(backend.closed)

    async def test_shutdown_closes_active_login_browsers(self):
        backend = FakeBackend()
        mgr = hs.HandshakeManager(Config(), lambda: backend, self._sink, memory_probe=lambda: None)
        s = await mgr.start("u1")
        await asyncio.sleep(0.05)
        await mgr.shutdown()
        self.assertEqual(s.state, "cancelled")
        self.assertTrue(backend.closed)


class CgroupAndQuietHandlerTests(unittest.TestCase):
    def test_cgroup_v2_free_memory_ignores_reclaimable_cache(self):
        with tempfile.TemporaryDirectory() as d:
            for name, value in (("memory.max", str(512 * bm.MB)), ("memory.current", str(400 * bm.MB)),
                                ("memory.stat", f"anon 1\ninactive_file {100 * bm.MB}\n")):
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(value)
            self.assertEqual(bm.container_memory(d), {"limit_mb": 512, "used_mb": 300, "free_mb": 212})

    def test_target_closed_future_is_not_reported_but_other_errors_are(self):
        TargetClosedError = type("TargetClosedError", (Exception,), {})
        seen = []
        loop = asyncio.new_event_loop()
        try:
            loop.set_exception_handler(lambda _l, ctx: seen.append(ctx["exception"]))
            bm.install_quiet_playwright_handler(loop)
            bm.install_quiet_playwright_handler(loop)       # idempotent
            handler = loop.get_exception_handler()
            handler(loop, {"message": "Future exception was never retrieved",
                           "exception": TargetClosedError("Target page, context or browser has been closed")})
            other = ValueError("real bug")
            handler(loop, {"message": "x", "exception": other})
        finally:
            loop.close()
        self.assertEqual(seen, [other])


if __name__ == "__main__":
    unittest.main()

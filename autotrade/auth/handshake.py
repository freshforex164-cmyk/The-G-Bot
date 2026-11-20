"""Automated session handshake.

The user logs in themselves (email/password, Google, CAPTCHA, 2FA) inside an isolated browser
that runs on the server; the frontend shows it through ``screen`` + ``input`` endpoints. We never
read the password field: we only watch the WebSocket auth frame the Pocket Option page sends
after login (``42["auth",{...}]``), extract the Demo and Real SSIDs, close the browser, and hand
the tokens to the vault.

NOT VERIFIED against the live site (needs a human login): login/cabinet URLs, allowed hosts and
how the page switches accounts. They are constants/config below so they can be adjusted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import unquote, urlparse

from ..clock import Clock
from ..config import Config
from .ssid import DEMO_KIND, KINDS, REAL_KIND, account_kind, build_ssid, parse_ssid, with_kind

log = logging.getLogger("autotrade.handshake")

LOGIN_URL = "https://pocketoption.com/en/login/"
CABINET_URL = {  # which page makes the web app connect as that account type (adjust if the site changes)
    DEMO_KIND: "https://pocketoption.com/en/cabinet/demo-quick-high-low/",
    REAL_KIND: "https://pocketoption.com/en/cabinet/quick-high-low/",
}
ALLOWED_HOSTS = ("pocketoption.com", "po.market", "po.cash", "google.com", "gstatic.com", "recaptcha.net",
                 "googleusercontent.com", "youtube.com")
VIEWPORT = {"width": 1100, "height": 760}
ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space"}


class CaptureBackend(Protocol):
    async def open(self) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def interact(self, event: dict[str, Any]) -> None: ...
    async def capture_tokens(self, timeout: float, on_status: Callable[[str], None]) -> tuple[dict[str, str], dict[str, Any]]: ...
    async def close(self) -> None: ...


def host_allowed(url: str, *, navigation: bool, hosts: tuple[str, ...] = ALLOWED_HOSTS) -> bool:
    """Block the isolated browser from reaching local/private addresses at all, and restrict
    top-level navigation to the login providers."""
    p = urlparse(url)
    if p.scheme in ("data", "blob", "about"):
        return True
    if p.scheme not in ("http", "https", "ws", "wss"):
        return False
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass
    if navigation:
        return any(host == h or host.endswith("." + h) for h in hosts)
    return True


class PlaywrightBackend:
    def __init__(self, headless: bool = True, hosts: tuple[str, ...] = ALLOWED_HOSTS):
        self._headless, self._hosts = headless, hosts
        self._pw = self._browser = self._context = None
        self._pages: list[Any] = []
        self._frames: dict[str, str] = {}
        self._event = asyncio.Event()

    async def open(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        # A fresh, non-persistent context per handshake: no shared cookies, nothing written to disk.
        self._context = await self._browser.new_context(viewport=VIEWPORT, locale="en-US", accept_downloads=False)
        await self._context.route("**/*", self._route)
        self._context.on("page", self._attach)
        page = await self._context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

    async def _route(self, route, request) -> None:
        nav = request.is_navigation_request()
        if host_allowed(request.url, navigation=nav, hosts=self._hosts):
            await route.continue_()
        else:
            await route.abort()

    def _attach(self, page) -> None:
        self._pages.append(page)
        page.on("websocket", lambda ws: ws.on("framesent", self._on_frame))
        page.on("close", lambda _p=page: self._pages.remove(_p) if _p in self._pages else None)

    def _on_frame(self, payload) -> None:
        if not isinstance(payload, str) or not payload.startswith('42["auth"'):
            return
        body = parse_ssid(payload)
        kind = account_kind(payload)
        if body is None or kind is None:
            return
        try:
            uid = int(body.get("uid") or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid > 0:   # frames without a real account id are the logged-out/guest page
            self._frames[kind] = payload
            self._event.set()

    @property
    def _page(self):
        return self._pages[-1] if self._pages else None

    async def screenshot(self) -> bytes:
        page = self._page
        if page is None:
            raise RuntimeError("browser is not open")
        return await page.screenshot(type="jpeg", quality=60)

    async def interact(self, event: dict[str, Any]) -> None:
        page = self._page
        if page is None:
            return
        kind = event.get("type")
        if kind == "click":
            x = min(max(int(event["x"]), 0), VIEWPORT["width"] - 1)
            y = min(max(int(event["y"]), 0), VIEWPORT["height"] - 1)
            await page.mouse.click(x, y)
        elif kind == "text":
            await page.keyboard.insert_text(str(event["text"])[:256])   # never logged
        elif kind == "key" and event.get("key") in ALLOWED_KEYS:
            await page.keyboard.press(str(event["key"]))
        elif kind == "scroll":
            await page.mouse.wheel(0, max(-2000, min(2000, int(event.get("dy", 0)))))

    async def capture_tokens(self, timeout, on_status):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        on_status("waiting_for_login")
        while not self._frames:
            remaining = deadline - loop.time()
            if remaining <= 0:
                cookie = await self._session_cookie()
                if cookie:  # fallback: same method as BinaryOptionsToolsV2.login()
                    return ({DEMO_KIND: build_ssid(cookie, DEMO_KIND), REAL_KIND: build_ssid(cookie, REAL_KIND)},
                            {"method": "cookie", "derived": list(KINDS)})
                raise TimeoutError("login was not completed in time")
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
        on_status("capturing")
        have = next(iter(self._frames))
        other = REAL_KIND if have == DEMO_KIND else DEMO_KIND
        # Ask the web app to connect as the other account type to capture its genuine frame.
        self._event.clear()
        try:
            await self._page.goto(CABINET_URL[other], wait_until="domcontentloaded", timeout=30000)
            end = loop.time() + 20
            while other not in self._frames and loop.time() < end:
                await asyncio.sleep(0.5)
        except Exception:
            log.info("could not switch account type in the browser; deriving instead")
        derived: list[str] = []
        if other not in self._frames:
            alt = with_kind(self._frames[have], other)   # same session, other isDemo flag
            if alt:
                self._frames[other] = alt
                derived.append(other)
        tokens = {k: self._frames[k] for k in KINDS if k in self._frames}
        return tokens, {"method": "websocket", "derived": derived}

    async def _session_cookie(self) -> str | None:
        if self._context is None:
            return None
        for c in await self._context.cookies():
            if c.get("name") == "po_session" and c.get("value"):
                return unquote(c["value"]) if "%" in c["value"] else c["value"]
        return None

    async def close(self) -> None:
        self._frames.clear()
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        self._pw = self._browser = self._context = None
        self._pages.clear()


@dataclass
class HandshakeSession:
    id: str
    user_id: str
    state: str = "starting"   # starting | waiting_for_login | capturing | done | failed | timeout | cancelled
    error: str | None = None
    created_at: datetime | None = None
    backend: CaptureBackend | None = None
    task: asyncio.Task | None = None
    kinds: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ("starting", "waiting_for_login", "capturing")


TokenSink = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[None]]


class HandshakeManager:
    def __init__(self, cfg: Config, backend_factory: Callable[[], CaptureBackend], sink: TokenSink, clock: Clock | None = None):
        self._cfg, self._factory, self._sink = cfg, backend_factory, sink
        self._clock = clock or Clock()
        self._sessions: dict[str, HandshakeSession] = {}

    def _prune(self) -> None:
        now = self._clock.utcnow()
        for sid, s in list(self._sessions.items()):
            if not s.active and s.created_at and (now - s.created_at).total_seconds() > 900:
                del self._sessions[sid]

    async def start(self, user_id: str) -> HandshakeSession:
        self._prune()
        for s in list(self._sessions.values()):
            if s.user_id == user_id and s.active:
                await self.cancel(s.id, user_id)
        if sum(1 for s in self._sessions.values() if s.active) >= self._cfg.max_handshakes:
            raise RuntimeError("too many logins in progress, try again shortly")
        session = HandshakeSession(secrets.token_urlsafe(24), user_id, created_at=self._clock.utcnow(), backend=self._factory())
        self._sessions[session.id] = session
        session.task = asyncio.create_task(self._run(session), name=f"handshake:{session.id[:6]}")
        return session

    def get(self, session_id: str, user_id: str) -> HandshakeSession:
        s = self._sessions.get(session_id)
        if s is None or s.user_id != user_id:   # same answer for "missing" and "someone else's"
            raise KeyError(session_id)
        return s

    async def screenshot(self, session_id: str, user_id: str) -> bytes:
        s = self.get(session_id, user_id)
        if not s.active or s.backend is None:
            raise RuntimeError("login session is not active")
        return await s.backend.screenshot()

    async def interact(self, session_id: str, user_id: str, event: dict[str, Any]) -> None:
        s = self.get(session_id, user_id)
        if not s.active or s.backend is None:
            raise RuntimeError("login session is not active")
        await s.backend.interact(event)

    async def cancel(self, session_id: str, user_id: str) -> None:
        s = self.get(session_id, user_id)
        if s.active:
            s.state = "cancelled"
            if s.task:
                s.task.cancel()
                await asyncio.gather(s.task, return_exceptions=True)
        if s.backend is not None:   # cancelled before the task ran: still make sure the browser is gone
            backend, s.backend = s.backend, None
            await backend.close()

    async def _run(self, s: HandshakeSession) -> None:
        backend = s.backend
        assert backend is not None
        tokens: dict[str, str] = {}
        meta: dict[str, Any] = {}
        try:
            await backend.open()
            s.state = "waiting_for_login"
            tokens, meta = await backend.capture_tokens(self._cfg.handshake_timeout, lambda st: setattr(s, "state", st))
        except asyncio.CancelledError:
            s.state = "cancelled"
            return
        except TimeoutError:
            s.state, s.error = "timeout", "login was not completed in time"
            return
        except Exception as exc:
            s.state, s.error = "failed", type(exc).__name__
            log.warning("handshake failed: %s", type(exc).__name__)
            return
        finally:
            await asyncio.shield(backend.close())      # browser is closed before anything else happens
            s.backend = None
        try:
            await self._sink(s.user_id, tokens, meta)
            s.kinds, s.state = sorted(tokens), "done"
        except Exception as exc:
            s.state, s.error = "failed", type(exc).__name__
            log.warning("could not store captured tokens: %s", type(exc).__name__)
        finally:
            tokens.clear()

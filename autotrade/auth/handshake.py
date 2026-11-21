"""Automated session handshake.

The user logs in themselves (email/password, Google, CAPTCHA, 2FA) on the real Pocket Option site,
rendered as a phone-sized page by an isolated browser on the server. The frontend shows it edge to
edge through ``screen`` (picture) + ``view`` (where the focused input is) + ``input`` endpoints, and
puts a real native <input> over whichever field is focused, so the phone's own keyboard, autofill and
password manager work directly in the site's email/password fields. The standard hosted form forwards
credentials transiently to that broker page; passwords are never logged, returned, or persisted. We watch the WebSocket auth frame the Pocket Option page sends
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
import re

from ..clock import Clock
from ..config import Config
from .ssid import DEMO_KIND, KINDS, REAL_KIND, account_kind, build_ssid, parse_ssid
from .session_state import broker_domain, sanitize_browser_session

log = logging.getLogger("autotrade.handshake")

LOGIN_URL = "https://pocketoption.com/en/login/"
CABINET_URL = {  # which page makes the web app connect as that account type (adjust if the site changes)
    DEMO_KIND: "https://pocketoption.com/en/cabinet/demo-quick-high-low/",
    REAL_KIND: "https://pocketoption.com/en/cabinet/quick-high-low/",
}
ALLOWED_HOSTS = ("pocketoption.com", "po.market", "po.cash", "google.com", "gstatic.com", "recaptcha.net",
                 "googleusercontent.com", "youtube.com")
BROKER_HOSTS = ("pocketoption.com", "po.market", "po.cash")
# Google sign-in and CAPTCHA must render fully, or the user cannot solve a challenge.
VERIFICATION_HOSTS = ("google.com", "gstatic.com", "recaptcha.net", "googleusercontent.com")
# Low-memory mode (Render 512 MB): one Chromium process, small JS heap, no GPU/shared-memory use.
LOW_MEMORY_ARGS = (
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--single-process", "--no-zygote", "--js-flags=--max-old-space-size=256",
    "--enable-low-end-device-mode", "--disable-software-rasterizer", "--disable-extensions",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-sync", "--mute-audio", "--no-first-run", "--disable-breakpad",
    "--disable-features=site-per-process,IsolateOrigins,Translate,BackForwardCache,MediaRouter,OptimizationHints",
    "--renderer-process-limit=1",
)
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media", "stylesheet"})
CLOSE_TIMEOUT = 10.0
VIEWPORT = {"width": 390, "height": 844}     # a phone: the site serves its mobile login layout
DEVICE_SCALE = 2
LOW_MEMORY_DEVICE_SCALE = 1
MIN_VIEW, MAX_VIEW = (320, 480), (520, 1100)   # accepted phone screen sizes (CSS px)
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/130.0.0.0 Mobile Safari/537.36")
MAX_VALUE_LEN = 256
# Describes the focused editable element (position in CSS px of the phone viewport). The value is only
# returned for non-password fields so the phone-side input can continue where the text left off.
FOCUS_JS = """() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el) return null;
  const tag = el.tagName;
  const editable = el.isContentEditable || tag === 'TEXTAREA' ||
    (tag === 'INPUT' && !['button','submit','checkbox','radio','file','image','reset','range','color'].includes((el.type || 'text').toLowerCase()));
  if (!editable) return null;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return null;
  const type = tag === 'INPUT' ? (el.type || 'text').toLowerCase() : 'text';
  return { x: r.left, y: r.top, w: r.width, h: r.height, type,
           name: String(el.name || el.id || '').slice(0, 40),
           value: type === 'password' ? '' : String(el.value || el.textContent || '').slice(0, 256),
           filled: !!(el.value || el.textContent) };
}"""
# Visible text boxes on the page, so a tap on one can open the phone keyboard immediately. No values.
INPUTS_JS = """() => {
  const out = [];
  const skip = ['button','submit','checkbox','radio','file','image','reset','range','color','hidden'];
  for (const el of document.querySelectorAll('input, textarea, [contenteditable=""], [contenteditable="true"]')) {
    if (el.tagName === 'INPUT' && skip.includes((el.type || 'text').toLowerCase())) continue;
    if (el.disabled || el.readOnly) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    out.push({ x: r.left, y: r.top, w: r.width, h: r.height,
               type: el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : 'text',
               name: String(el.name || el.id || el.getAttribute('autocomplete') || '').slice(0, 40) });
    if (out.length >= 12) break;
  }
  return out;
}"""
# Visible broker-side feedback after a sign-in attempt (wrong password, CAPTCHA, blocked account).
# Text only, from alert/error elements; never input values.
NOTICE_JS = """() => {
  const sel = '.alert, .error, .errors, .invalid-feedback, .help-block, .form-error, [role=alert], [class*="error"], [class*="alert"]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('input, textarea, select, script, style')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length >= 4) return t.slice(0, 160);
  }
  return null;
}"""
ERROR_MESSAGES = {
    "broker_unreachable": "Pocket Option did not respond in time. Try again in a minute.",
    "browser_unavailable": "The secure sign-in browser could not start on the server. Try again in a minute.",
    "browser_crashed": "The secure sign-in browser stopped unexpectedly (the server ran short of memory). "
                       "Nothing was saved. Try again in a minute.",
    "login_timeout": "The sign-in took too long and was closed. Send /at_login for a new link.",
    "store_failed": "You signed in, but the session could not be saved. Send /at_login to try again.",
    "capture_failed": "The sign-in could not be completed. Send /at_login to try again.",
}
ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space"}


class CaptureBackend(Protocol):
    async def open(self) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def view(self) -> dict[str, Any]: ...
    async def interact(self, event: dict[str, Any]) -> None: ...
    async def submit_credentials(self, email: str, password: str) -> None: ...
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


class BrowserCrashed(RuntimeError):
    """Chromium died (usually out of memory) while the login was in progress."""


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def launch_args(low_memory: bool) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if low_memory:
        args += list(LOW_MEMORY_ARGS)
    return args


def should_block(url: str, resource_type: str, *, block_assets: bool, block_third_party: bool) -> bool:
    """Login only needs the broker's HTML and scripts. Pictures, fonts, media and stylesheets (and
    third-party trackers) are dropped to keep Chromium small; Google/CAPTCHA pages stay complete."""
    p = urlparse(url)
    if p.scheme in ("data", "blob", "about"):
        return False
    host = (p.hostname or "").lower()
    if _host_in(host, VERIFICATION_HOSTS):
        return False
    if block_assets and resource_type in BLOCKED_RESOURCE_TYPES:
        return True
    return block_third_party and not _host_in(host, BROKER_HOSTS)


class PlaywrightBackend:
    def __init__(self, headless: bool = True, hosts: tuple[str, ...] = ALLOWED_HOSTS, login_url: str = LOGIN_URL,
                 *, low_memory: bool = True, block_assets: bool = True, block_third_party: bool = True):
        self._headless, self._hosts, self._login_url = headless, hosts, login_url
        self._low_memory, self._block_assets, self._block_third_party = low_memory, block_assets, block_third_party
        # Login-page assets are dropped only while the user is not looking at the broker page (email/password
        # form). Once they must see it (verification after submit, or "finish on the Pocket Option page"),
        # it loads styled. Trading cabinet pages are only visited to capture sessions, so stay stripped.
        self._strip_login_page = block_assets
        self._vp = dict(VIEWPORT)
        self._pw = self._browser = self._context = None
        self._pages: list[Any] = []
        self._frames: dict[str, str] = {}
        self._event = asyncio.Event()
        self._credential_lock = asyncio.Lock()
        self._crashed = False

    def set_viewport(self, width: int, height: int) -> None:
        """Match the user's own screen so the site fills the phone edge to edge. Before open()."""
        self._vp = {"width": min(max(int(width), MIN_VIEW[0]), MAX_VIEW[0]),
                    "height": min(max(int(height), MIN_VIEW[1]), MAX_VIEW[1])}

    def show_full_page(self) -> None:
        """The user is looking at / using the broker page itself: load it with its styles and pictures."""
        self._strip_login_page = False

    def _strip_assets(self) -> bool:
        if not self._block_assets:
            return False
        if self._strip_login_page:
            return True
        page = self._page
        return page is not None and "/cabinet" in (page.url or "")

    async def open(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, args=launch_args(self._low_memory), timeout=60000)
        self._browser.on("disconnected", lambda _b: self._mark_crashed())
        # A fresh, non-persistent context per handshake: no shared cookies, nothing written to disk.
        # Phone emulation (touch, mobile user agent) makes the site serve its standard mobile web layout.
        self._context = await self._browser.new_context(
            viewport=self._vp, device_scale_factor=LOW_MEMORY_DEVICE_SCALE if self._low_memory else DEVICE_SCALE,
            is_mobile=True, has_touch=True, user_agent=MOBILE_UA, locale="en-US", accept_downloads=False,
            service_workers="block")
        await self._context.route("**/*", self._route)
        self._context.on("page", self._attach)
        page = await self._context.new_page()
        # "commit" returns as soon as the broker answers; slow scripts/ads must not fail the login.
        await page.goto(self._login_url, wait_until="commit", timeout=60000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            log.info("broker login page is still loading; continuing")

    async def _route(self, route, request) -> None:
        nav = request.is_navigation_request()
        try:
            if not host_allowed(request.url, navigation=nav, hosts=self._hosts) or (
                    not nav and should_block(request.url, request.resource_type, block_assets=self._strip_assets(),
                                             block_third_party=self._block_third_party)):
                await route.abort()
            else:
                await route.continue_()
        except Exception:       # page closed while routing: nothing left to do
            pass

    def _mark_crashed(self) -> None:
        if self._browser is not None:     # close() clears it first, so a normal shutdown is not a crash
            self._crashed = True
            self._event.set()

    def _check_alive(self) -> None:
        if self._crashed:
            raise BrowserCrashed("the browser stopped")

    def _attach(self, page) -> None:
        self._pages.append(page)
        page.on("crash", lambda _p: self._mark_crashed())
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
        return await page.screenshot(type="jpeg", quality=50 if self._low_memory else 60)

    async def view(self) -> dict[str, Any]:
        """Phone viewport size, the focused input (if any) and which site is showing. Never a password."""
        page = self._page
        if page is None:
            raise RuntimeError("browser is not open")
        try:
            field = await page.evaluate(FOCUS_JS)
            inputs = await page.evaluate(INPUTS_JS)
        except Exception:      # page is navigating
            field, inputs = None, []
        notice = None
        if "login" in (urlparse(page.url).path if page.url else ""):   # cabinet banners are not login feedback
            try:
                notice = await page.evaluate(NOTICE_JS)
            except Exception:
                notice = None
        host = (urlparse(page.url).hostname or "") if page.url else ""
        return {"width": self._vp["width"], "height": self._vp["height"], "field": field,
                "inputs": inputs, "host": host, "notice": notice}

    async def interact(self, event: dict[str, Any]) -> None:
        self.show_full_page()       # the user is using the broker page directly
        page = self._page
        if page is None:
            return
        kind = event.get("type")
        if kind == "click":
            x = min(max(int(event["x"]), 0), self._vp["width"] - 1)
            y = min(max(int(event["y"]), 0), self._vp["height"] - 1)
            await page.touchscreen.tap(x, y)
        elif kind == "value":
            # The phone-side input holds the whole text: replace the focused field's content with it.
            # Typed text goes straight to the site's own field and is never logged or stored.
            if await page.evaluate(FOCUS_JS) is None:
                return
            text = str(event.get("value", ""))[:MAX_VALUE_LEN]
            await page.keyboard.press("Control+A")
            if text:
                await page.keyboard.insert_text(text)
            else:
                await page.keyboard.press("Backspace")
        elif kind == "text":
            await page.keyboard.insert_text(str(event["text"])[:MAX_VALUE_LEN])
        elif kind == "key" and event.get("key") in ALLOWED_KEYS:
            await page.keyboard.press(str(event["key"]))
        elif kind == "scroll":
            x = min(max(int(event.get("x", self._vp["width"] // 2)), 0), self._vp["width"] - 1)
            y = min(max(int(event.get("y", self._vp["height"] // 2)), 0), self._vp["height"] - 1)
            await page.mouse.move(x, y)
            await page.mouse.wheel(0, max(-2000, min(2000, int(event.get("dy", 0)))))

    async def capture_tokens(self, timeout, on_status):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        on_status("waiting_for_login")
        while not self._frames:
            self._check_alive()
            remaining = deadline - loop.time()
            if remaining <= 0:
                cookie = await self._session_cookie()
                if cookie:  # fallback: same method as BinaryOptionsToolsV2.login()
                    return ({DEMO_KIND: build_ssid(cookie, DEMO_KIND), REAL_KIND: build_ssid(cookie, REAL_KIND)},
                            await self._capture_metadata("cookie", list(KINDS)))
                raise TimeoutError("login was not completed in time")
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
        self._check_alive()
        on_status("capturing")
        have = next(iter(self._frames))
        other = REAL_KIND if have == DEMO_KIND else DEMO_KIND
        # Ask the web app to connect as the other account type to capture its genuine frame. Demo and Real
        # use different session values (verified live), so the other kind can never be derived from this one.
        self._event.clear()
        try:
            await self._page.goto(CABINET_URL[other], wait_until="domcontentloaded", timeout=30000)
            end = loop.time() + 20
            while other not in self._frames and loop.time() < end and not self._crashed:
                await asyncio.sleep(0.25)
        except Exception:
            log.info("could not switch account type in the browser; keeping the %s session only", have)
        if self._crashed and not self._frames:
            raise BrowserCrashed("the browser stopped")
        tokens = {k: self._frames[k] for k in KINDS if k in self._frames}
        return tokens, await self._capture_metadata("websocket", [])

    async def _capture_metadata(self, method, derived):
        try:
            state = {"cookies": await self._context.cookies()} if self._context else {"cookies": []}
        except Exception:      # browser already gone: the session frames are what matter
            state = {"cookies": []}
        return {"method": method, "derived": derived, "_browser_session": sanitize_browser_session(state)}

    async def submit_credentials(self, email: str, password: str) -> None:
        """Submit only to the existing HTTPS broker page. CAPTCHA/2FA remain interactive."""
        if not (isinstance(email, str) and len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise RuntimeError("Enter a valid email address.")
        if not isinstance(password, str) or not 1 <= len(password) <= MAX_VALUE_LEN:
            raise RuntimeError("Enter a valid password.")
        # Whatever happens next (verification, or "use the broker page"), the user now sees that page.
        self.show_full_page()
        async with self._credential_lock:
            page = self._page
            url = urlparse(page.url) if page is not None else None
            if url is None or url.scheme != "https" or not broker_domain(url.hostname or ""):
                raise RuntimeError("The broker login page is not ready.")
            passwords = page.locator('input[type="password"]:visible')
            if await passwords.count() != 1:
                raise RuntimeError("Use the broker page to complete this login.")
            password_field = passwords.first
            form = password_field.locator("xpath=ancestor::form[1]")
            if await form.count() != 1:
                raise RuntimeError("Use the broker page to complete this login.")
            emails = form.locator('input[type="email"]:visible, input[name="email"]:visible, input[name="login"]:visible, input[name="username"]:visible')
            if await emails.count() != 1:
                raise RuntimeError("Use the broker page to complete this login.")
            try:
                await emails.fill(email)
                await password_field.fill(password)
                await password_field.press("Enter")
            except Exception:
                raise RuntimeError("Could not submit the broker login. Use the broker page to continue.") from None
            finally:
                password = ""
                try:
                    if not page.is_closed() and await password_field.count():
                        await password_field.fill("", timeout=1000)
                except Exception:
                    pass

    async def _session_cookie(self) -> str | None:
        if self._context is None:
            return None
        for c in await self._context.cookies():
            if c.get("name") == "po_session" and c.get("value") and broker_domain(c.get("domain", "")):
                return unquote(c["value"]) if "%" in c["value"] else c["value"]
        return None

    async def close(self) -> None:
        """Always frees Chromium, even if it hangs: every step is time-boxed and stopping Playwright
        kills any browser process that is still alive."""
        self._frames.clear()
        context, browser, pw = self._context, self._browser, self._pw
        self._pw = self._browser = self._context = None       # from here on a disconnect is not a crash
        pages, self._pages = list(self._pages), []
        steps = [(p, "close") for p in pages] + [(context, "close"), (browser, "close"), (pw, "stop")]
        for obj, method in steps:
            if obj is None:
                continue
            try:
                await asyncio.wait_for(getattr(obj, method)(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass


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

    @property
    def message(self) -> str | None:
        """User-facing explanation of a failed/timed-out login (never internal details)."""
        if self.state == "timeout":
            return ERROR_MESSAGES["login_timeout"]
        if self.state == "failed":
            return ERROR_MESSAGES.get(self.error or "", ERROR_MESSAGES["capture_failed"])
        return None


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

    async def start(self, user_id: str, viewport: tuple[int, int] | None = None,
                    full_view: bool = False) -> HandshakeSession:
        self._prune()
        for s in list(self._sessions.values()):
            if s.user_id == user_id and s.active:
                await self.cancel(s.id, user_id)
        if sum(1 for s in self._sessions.values() if s.active) >= self._cfg.max_handshakes:
            raise RuntimeError("too many logins in progress, try again shortly")
        session = HandshakeSession(secrets.token_urlsafe(24), user_id, created_at=self._clock.utcnow(), backend=self._factory())
        if viewport and hasattr(session.backend, "set_viewport"):
            session.backend.set_viewport(*viewport)
        if full_view and hasattr(session.backend, "show_full_page"):
            session.backend.show_full_page()
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

    async def view(self, session_id: str, user_id: str) -> dict[str, Any]:
        s = self.get(session_id, user_id)
        if not s.active or s.backend is None:
            raise RuntimeError("login session is not active")
        return await s.backend.view()

    async def interact(self, session_id: str, user_id: str, event: dict[str, Any]) -> None:
        s = self.get(session_id, user_id)
        if not s.active or s.backend is None:
            raise RuntimeError("login session is not active")
        await s.backend.interact(event)

    async def submit_credentials(self, session_id: str, user_id: str, email: str, password: str) -> None:
        s = self.get(session_id, user_id)
        if s.state != "waiting_for_login" or s.backend is None:
            raise RuntimeError("The broker login page is not ready.")
        await s.backend.submit_credentials(email, password)

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
            try:
                await backend.open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                name = type(exc).__name__
                text = str(exc).lower()
                # Playwright's TimeoutError is not the builtin one; network errors arrive as "net::ERR_*".
                unreachable = "timeout" in name.lower() or "net::" in text or "err_" in text
                crashed = isinstance(exc, BrowserCrashed) or "crash" in text or "closed" in text
                s.state, s.error = "failed", ("broker_unreachable" if unreachable else
                                              "browser_crashed" if crashed else "browser_unavailable")
                log.warning("handshake browser could not open the broker page: %s", name)
                return
            s.state = "waiting_for_login"
            tokens, meta = await backend.capture_tokens(self._cfg.handshake_timeout, lambda st: setattr(s, "state", st))
        except asyncio.CancelledError:
            s.state = "cancelled"
            return
        except BrowserCrashed:
            s.state, s.error = "failed", "browser_crashed"
            log.warning("handshake browser crashed (likely out of memory)")
            return
        except TimeoutError:
            s.state, s.error = "timeout", "login_timeout"
            return
        except Exception as exc:
            crashed = "closed" in str(exc).lower() or "crash" in str(exc).lower()
            s.state, s.error = "failed", "browser_crashed" if crashed else "capture_failed"
            log.warning("handshake failed: %s", type(exc).__name__)
            return
        finally:
            await asyncio.shield(backend.close())      # browser is closed before anything else happens
            s.backend = None
        try:
            await self._sink(s.user_id, tokens, meta)
            s.kinds, s.state = sorted(tokens), "done"
        except Exception as exc:
            s.state, s.error = "failed", "store_failed"
            log.warning("could not store captured tokens: %s", type(exc).__name__)
        finally:
            tokens.clear()

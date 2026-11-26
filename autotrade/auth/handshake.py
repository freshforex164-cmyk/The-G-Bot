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
from . import browser_memory as bm
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
# Optional low-memory mode: no GPU, no /dev/shm, small JS heap, no background services.
LOW_MEMORY_ARGS = (
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",                 # containers give /dev/shm only 64 MB: use /tmp instead
    "--disable-gpu", "--disable-gpu-compositing", "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    "--js-flags=--max-old-space-size=128 --optimize-for-size",
    "--enable-low-end-device-mode", "--force-device-scale-factor=1", "--aggressive-cache-discard",
    "--disable-extensions", "--disable-background-networking", "--disable-component-update",
    "--disable-default-apps", "--disable-sync", "--mute-audio", "--no-first-run", "--no-default-browser-check",
    "--disable-breakpad", "--disable-hang-monitor", "--disable-client-side-phishing-detection",
    "--disable-domain-reliability", "--disable-notifications", "--disable-speech-api", "--disable-print-preview",
    "--disable-site-isolation-trials",
    "--disable-features=site-per-process,IsolateOrigins,Translate,BackForwardCache,MediaRouter,OptimizationHints,"
    "AudioServiceOutOfProcess,PaintHolding,AutofillServerCommunication,CertificateTransparencyComponentUpdater",
)
# Everything in one OS process (browser + renderer + GPU thread): the biggest single saving. Chromium does not
# officially support it, so AUTOTRADE_CHROMIUM_SINGLE_PROCESS=false turns it off if a site crashes with it.
SINGLE_PROCESS_ARGS = ("--single-process", "--no-zygote", "--renderer-process-limit=1", "--in-process-gpu")
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media", "stylesheet"})
CLOSE_TIMEOUT = 10.0
SCREENSHOT_TIMEOUT = 8.0      # a frame that takes longer is skipped; the page keeps its last picture
FRAME_INTERVAL = 1.5          # low-memory: at most one new picture per 1.5 s (each one rasterises the page)
FAST_FRAME_INTERVAL = 0.5     # normal mode
TOUCHED_FRAME_INTERVAL = 0.35 # right after a tap/keystroke the next picture may come sooner
CHALLENGE_CHECK_INTERVAL = 2.0  # Turnstile/CAPTCHA/2FA detection reads page text: not on every poll
LEFTOVER_WAIT = 2.0           # after close(), how long browser processes get to exit before SIGKILL
IDLE_TIMEOUT = 150.0          # no poll from the phone for this long = page closed: free Chromium
CABINET_SWITCH_DELAY = 3.5   # seconds between Real and Demo cabinet pages (rapid switching -> "Error. Please try later.")
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
# Raw challenge signals only (iframe sources, a few selectors, page title/text excerpts); never input values.
# ``classify_challenge`` turns them into turnstile | captcha | two_factor so the logic is testable offline.
CHALLENGE_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width >= 40 && r.height >= 40 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const frames = Array.from(document.querySelectorAll('iframe')).filter(vis).map(f => String(f.src || '').slice(0, 300));
  const q = (s) => { try { return Array.from(document.querySelectorAll(s)).some(vis); } catch (e) { return false; } };
  return {
    frames: frames.slice(0, 20),
    title: String(document.title || '').slice(0, 120),
    text: String((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').slice(0, 4000),
    turnstile: q('.cf-turnstile, #challenge-form, #cf-challenge-running, #challenge-stage, [name="cf-turnstile-response"]'),
    captcha: q('.g-recaptcha, .h-captcha, #captcha, [class*="captcha" i]'),
    otp: q('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="2fa" i], input[name*="totp" i], input[name="code"], input[name*="verification" i]'),
  };
}"""
TWO_FACTOR_WORDS = ("two-factor", "two factor", "2fa", "verification code", "authenticator", "one-time code",
                    "code we sent", "code was sent", "enter the code", "confirmation code")
TURNSTILE_WORDS = ("verify you are human", "checking your browser", "checking if the site connection is secure",
                   "needs to review the security of your connection")
CHALLENGE_HINTS = {
    "turnstile": "Pocket Option is showing a Cloudflare security check. Tick \"Verify you are human\" on the "
                 "Pocket Option page and wait for it to finish.",
    "captcha": "Pocket Option is asking for a CAPTCHA. Solve the picture check on the Pocket Option page, then "
               "continue signing in.",
    "two_factor": "Pocket Option is asking for a two-factor (2FA) code. Type the code from your email or "
                  "authenticator app into the Pocket Option page.",
}


def classify_challenge(sig: dict[str, Any] | None) -> str | None:
    """turnstile | captcha | two_factor | None from CHALLENGE_JS signals. Invisible reCAPTCHA badges (always
    present on some login pages) are not a challenge; only visible challenge widgets count."""
    if not isinstance(sig, dict):
        return None
    frames = [str(f).lower() for f in (sig.get("frames") or [])]
    title = str(sig.get("title") or "").lower()
    text = str(sig.get("text") or "").lower()
    if (sig.get("turnstile") or any("challenges.cloudflare.com" in f or "turnstile" in f for f in frames)
            or "just a moment" in title or any(w in text for w in TURNSTILE_WORDS)):
        return "turnstile"
    if sig.get("otp") or any(w in text for w in TWO_FACTOR_WORDS):
        return "two_factor"
    visible_captcha = [f for f in frames if ("recaptcha" in f or "hcaptcha" in f) and "size=invisible" not in f]
    if sig.get("captcha") or visible_captcha:
        return "captcha"
    return None


ERROR_MESSAGES = {
    "broker_unreachable": "Pocket Option did not respond in time. Try again in a minute.",
    "browser_unavailable": "The secure sign-in browser could not start on the server. Try again in a minute.",
    "browser_crashed": "The secure sign-in browser stopped unexpectedly (the server ran short of memory). "
                       "Nothing was saved. Try again in a minute.",
    "low_memory": "The sign-in server is short of memory right now, so the secure browser was stopped before "
                  "it could crash the service (running trades are protected). Nothing was saved. Try again in a "
                  "minute; if this keeps happening the autotrade service needs more available VPS memory.",
    "broker_rate_limited": "Pocket Option is limiting sign-ins from the server right now (too many requests). "
                           "Nothing was saved. Wait a few minutes, then send /at_login again.",
    "login_timeout": "The sign-in took too long and was closed. Send /at_login for a new link.",
    "store_failed": "You signed in, but the session could not be saved. Send /at_login to try again.",
    "capture_failed": "The sign-in could not be completed. Send /at_login to try again.",
    "turnstile_blocked": "Pocket Option showed a Cloudflare security check (Turnstile) that was not completed, "
                         "so the sign-in was closed and nothing was saved. Wait 10-15 minutes, send /at_login, "
                         "and tick \"Verify you are human\" on the Pocket Option page as soon as it appears. "
                         "If the check keeps looping, Pocket Option is limiting the server; try again later.",
    "captcha_required": "Pocket Option asked for a CAPTCHA that was not solved in time, so the sign-in was closed "
                        "and nothing was saved. Send /at_login and solve the picture check on the Pocket Option "
                        "page before submitting.",
    "two_factor_required": "Pocket Option asked for a two-factor (2FA) code that was not entered in time, so the "
                           "sign-in was closed and nothing was saved. Send /at_login, sign in, and type the code "
                           "from your email or authenticator app into the Pocket Option page within a few minutes.",
}
CHALLENGE_ERRORS = {"turnstile": "turnstile_blocked", "captcha": "captcha_required", "two_factor": "two_factor_required"}
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


class BrokerRateLimited(RuntimeError):
    """Pocket Option answered 429 Too Many Requests (or 503 with Retry-After) during the sign-in."""

    def __init__(self, retry_after: float | None = None):
        super().__init__("broker rate limited the sign-in")
        self.retry_after = retry_after


class LoginCooldown(Exception):
    """A new sign-in was refused because the broker recently rate-limited or timed out (backoff)."""

    def __init__(self, wait: float, message: str):
        super().__init__(message)
        self.wait, self.message = wait, message


# Backoff after broker 429/timeouts: base * 2^n seconds, capped. Spacing between any two Chromium launches.
BACKOFF_BASE, BACKOFF_CAP, LAUNCH_SPACING = 60.0, 1800.0, 5.0


def backoff_delay(failures: int, retry_after: float | None = None) -> float:
    exp = BACKOFF_BASE * (2 ** max(0, failures - 1))
    return float(min(BACKOFF_CAP, max(exp, float(retry_after or 0))))


def _retry_after(headers: Any) -> float | None:
    try:
        v = (headers or {}).get("retry-after")
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


class ChallengeBlocked(TimeoutError):
    """The login timed out while the broker page was showing Turnstile, a CAPTCHA or a 2FA prompt."""

    def __init__(self, challenge: str):
        super().__init__(f"login blocked by {challenge}")
        self.challenge = challenge


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def launch_args(low_memory: bool, single_process: bool = True) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if low_memory:
        args += list(LOW_MEMORY_ARGS)
        if single_process:
            args += list(SINGLE_PROCESS_ARGS)
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
                 *, low_memory: bool = True, block_assets: bool = True, block_third_party: bool = True,
                 switch_delay: float = CABINET_SWITCH_DELAY, single_process: bool = True,
                 frame_interval: float | None = None):
        self._headless, self._hosts, self._login_url = headless, hosts, login_url
        self._switch_delay = max(0.0, float(switch_delay))
        self._low_memory, self._block_assets, self._block_third_party = low_memory, block_assets, block_third_party
        self._single_process = single_process
        self._frame_interval = max(0.0, float(frame_interval if frame_interval is not None else
                                              FRAME_INTERVAL if low_memory else FAST_FRAME_INTERVAL))
        self._frame, self._frame_at, self._touched = b"", 0.0, False
        self._frame_lock = asyncio.Lock()
        self._pids: set[int] = set()     # OS processes started for this login (Playwright driver + Chromium)
        self.leftovers_killed = 0        # processes close() had to SIGKILL (0 = everything exited by itself)
        self.closed = False
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
        self.rate_limited: float | None = None     # set (Retry-After seconds, 0 = unknown) on a broker 429
        self.challenge: str | None = None          # last verification step seen on the broker page
        self._on_challenge: Callable[[str | None], None] | None = None
        self._challenge_checked = 0.0

    def set_challenge_listener(self, fn: Callable[[str | None], None]) -> None:
        self._on_challenge = fn

    async def detect_challenge(self) -> str | None:
        """Look for Cloudflare Turnstile, CAPTCHA or 2FA on the visible broker page (best effort)."""
        page = self._page
        if page is None:
            return self.challenge
        try:
            found = classify_challenge(await page.evaluate(CHALLENGE_JS))
        except Exception:          # navigating: keep the last answer
            return self.challenge
        if found != self.challenge:
            if found:
                log.info("broker page shows a %s step", found)
            self.challenge = found
            if self._on_challenge is not None:
                try:
                    self._on_challenge(found)
                except Exception:
                    pass
        return found

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

        bm.install_quiet_playwright_handler()
        bm.release_memory()                  # give Chromium every free page before it starts
        before = bm.descendant_pids()
        self.closed = False
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self._headless, args=launch_args(self._low_memory, self._single_process), timeout=60000)
        finally:   # remember exactly which processes this login started, so close() can verify they are gone
            self._pids = bm.descendant_pids() - before
        self._browser.on("disconnected", self._on_disconnected)
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
        self._check_alive()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            log.info("broker login page is still loading; continuing")
        self._check_alive()

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

    def _on_disconnected(self, _browser) -> None:
        self._mark_crashed()

    def _mark_crashed(self) -> None:
        if self._browser is not None:     # close() clears it first, so a normal shutdown is not a crash
            self._crashed = True
            self._event.set()

    def _on_response(self, response) -> None:
        try:
            status = int(response.status)
            if status not in (429, 503):
                return
            host = (urlparse(response.url).hostname or "").lower()
            if not _host_in(host, BROKER_HOSTS):
                return
            ra = _retry_after(response.headers)
            if status == 503 and ra is None:
                return
            if self.rate_limited is None:
                log.warning("broker answered %d during sign-in", status)
            self.rate_limited = ra or 0.0
            self._event.set()
        except Exception:
            pass

    def _check_alive(self) -> None:
        if self.rate_limited is not None:
            raise BrokerRateLimited(self.rate_limited or None)
        if self._crashed:
            raise BrowserCrashed("the browser stopped")

    def _attach(self, page) -> None:
        self._pages.append(page)
        page.on("crash", lambda _p: self._mark_crashed())
        page.on("response", self._on_response)
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
        """The live picture for the phone. Every capture makes Chromium rasterise the whole page (a memory
        spike), so captures are throttled and serialised: polls arriving sooner get the last picture."""
        page = self._page
        if page is None:
            raise RuntimeError("browser is not open")
        self._check_alive()
        loop = asyncio.get_running_loop()
        if self._frame and loop.time() - self._frame_at < self._gap():
            return self._frame
        async with self._frame_lock:
            if self._frame and loop.time() - self._frame_at < self._gap():
                return self._frame                  # another poll captured it while we waited
            try:
                # Time-boxed and without waiting for animations: a busy or navigating page must never stall
                # the phone's live view (that showed as a black screen).
                shot = await page.screenshot(type="jpeg", quality=45 if self._low_memory else 60,
                                             timeout=SCREENSHOT_TIMEOUT * 1000, animations="disabled",
                                             caret="initial")
            except Exception:
                self._check_alive()                 # browser gone -> BrowserCrashed, not a stale picture
                if self._frame:
                    return self._frame              # navigating: keep showing the last picture
                raise
            self._frame, self._frame_at, self._touched = shot, loop.time(), False
            return shot

    def _gap(self) -> float:
        return min(self._frame_interval, TOUCHED_FRAME_INTERVAL) if self._touched else self._frame_interval

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
        loop = asyncio.get_running_loop()
        if loop.time() - self._challenge_checked >= CHALLENGE_CHECK_INTERVAL:
            self._challenge_checked = loop.time()
            challenge = await self.detect_challenge()
        else:
            challenge = self.challenge
        return {"width": self._vp["width"], "height": self._vp["height"], "field": field,
                "inputs": inputs, "host": host, "notice": notice, "challenge": challenge,
                "hint": CHALLENGE_HINTS.get(challenge or "")}

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
        self._touched = True        # show the result of the tap/keystroke soon

    async def capture_tokens(self, timeout, on_status):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        on_status("waiting_for_login")
        while not self._frames:
            self._check_alive()
            if loop.time() - self._challenge_checked >= 2.0:
                self._challenge_checked = loop.time()
                await self.detect_challenge()
            remaining = deadline - loop.time()
            if remaining <= 0:
                cookie = await self._session_cookie()
                if cookie:  # fallback: same method as BinaryOptionsToolsV2.login()
                    return ({DEMO_KIND: build_ssid(cookie, DEMO_KIND), REAL_KIND: build_ssid(cookie, REAL_KIND)},
                            await self._capture_metadata("cookie", list(KINDS)))
                await self.detect_challenge()
                if self.challenge:
                    raise ChallengeBlocked(self.challenge)
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
            # A natural pause first: switching cabinets within a second triggers the broker's rate limit.
            await asyncio.sleep(self._switch_delay)
            self._check_alive()
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
        """Always frees Chromium, even if it hangs. Order: stop routing and listeners, close every page
        (popups included), the context, the browser, then the Playwright driver; every step is time-boxed.
        Then it verifies that no process started for this login is still alive and SIGKILLs any that is."""
        self._frames.clear()
        self._frame = b""
        context, browser, pw = self._context, self._browser, self._pw
        self._pw = self._browser = self._context = None       # from here on a disconnect is not a crash
        pages, self._pages = list(self._pages), []
        pids, self._pids = set(self._pids), set()
        if context is not None:
            for event, fn in (("page", self._attach),):
                try:
                    context.remove_listener(event, fn)
                except Exception:
                    pass
            try:   # pending route handlers would otherwise fail after the browser is gone
                await asyncio.wait_for(context.unroute_all(behavior="ignoreErrors"), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
        if browser is not None:
            try:
                browser.remove_listener("disconnected", self._on_disconnected)
            except Exception:
                pass
        steps = [(p, "close") for p in pages] + [(context, "close"), (browser, "close"), (pw, "stop")]
        for obj, method in steps:
            if obj is None:
                continue
            try:
                await asyncio.wait_for(getattr(obj, method)(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
        self.leftovers_killed = await self._verify_exited(pids)
        self.closed = True
        bm.release_memory()

    @staticmethod
    async def _verify_exited(pids: set[int]) -> int:
        if not pids:
            return 0
        loop = asyncio.get_running_loop()
        end = loop.time() + LEFTOVER_WAIT
        while loop.time() < end and any(bm.alive(p) for p in pids):
            await asyncio.sleep(0.1)
        killed = bm.reap(pids)
        if killed:
            log.warning("killed %d login browser process(es) that did not exit after close", len(killed))
        return len(killed)


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
    challenge: str | None = None     # turnstile | captcha | two_factor while the broker page shows one
    replace: bool = False            # "Sign in again anyway": this login replaces every saved session
    retry_after: float | None = None  # broker Retry-After seconds on a 429
    last_seen: float = 0.0           # loop time of the phone's last poll (abandoned pages are closed)

    @property
    def hint(self) -> str | None:
        """What the user must do on the broker page right now (None when nothing is blocking)."""
        return CHALLENGE_HINTS.get(self.challenge or "") if self.active else None

    @property
    def active(self) -> bool:
        return self.state in ("starting", "waiting_for_login", "capturing")

    @property
    def message(self) -> str | None:
        """User-facing explanation of a failed/timed-out login (never internal details)."""
        if self.state == "timeout":
            return ERROR_MESSAGES.get(self.error or "", ERROR_MESSAGES["login_timeout"])
        if self.state == "failed":
            return ERROR_MESSAGES.get(self.error or "", ERROR_MESSAGES["capture_failed"])
        return None


# Returns the account kinds actually stored (None = all captured kinds).
TokenSink = Callable[[str, dict[str, str], dict[str, Any]], Awaitable["list[str] | None"]]
FailureNotice = Callable[[str, str], None]     # (user_id, user-facing message), e.g. a Telegram alert


class HandshakeManager:
    def __init__(self, cfg: Config, backend_factory: Callable[[], CaptureBackend], sink: TokenSink, clock: Clock | None = None,
                 on_failure: FailureNotice | None = None,
                 memory_probe: Callable[[], "dict[str, int] | None"] | None = None):
        self._cfg, self._factory, self._sink = cfg, backend_factory, sink
        self._memory = memory_probe or bm.container_memory
        self._on_failure = on_failure
        self._clock = clock or Clock()
        self._sessions: dict[str, HandshakeSession] = {}
        self._failures: dict[str, int] = {}          # user -> consecutive broker 429/timeout failures
        self._cooldown_until: dict[str, float] = {}  # user -> loop time a new sign-in is allowed
        self._global_until = 0.0                     # whole server backs off after any broker 429
        self._last_launch: float | None = None
        self._launch_lock = asyncio.Lock()

    def cooldown_left(self, user_id: str) -> float:
        now = asyncio.get_running_loop().time()
        return max(0.0, self._cooldown_until.get(user_id, 0.0) - now, self._global_until - now)

    def _note_result(self, s: "HandshakeSession", retry_after: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        if s.state == "done":
            self._failures.pop(s.user_id, None)
            self._cooldown_until.pop(s.user_id, None)
            return
        if s.error not in ("broker_rate_limited", "broker_unreachable"):
            return
        n = self._failures.get(s.user_id, 0) + 1
        self._failures[s.user_id] = n
        delay = backoff_delay(n, retry_after)
        self._cooldown_until[s.user_id] = loop.time() + delay
        if s.error == "broker_rate_limited":
            self._global_until = max(self._global_until, loop.time() + backoff_delay(1, retry_after))
        log.info("sign-in backoff %.0fs after %s (attempt %d)", delay, s.error, n)

    def _prune(self) -> None:
        now = self._clock.utcnow()
        for sid, s in list(self._sessions.items()):
            if not s.active and s.created_at and (now - s.created_at).total_seconds() > 900:
                del self._sessions[sid]

    async def start(self, user_id: str, viewport: tuple[int, int] | None = None,
                    full_view: bool = False, replace: bool = False) -> HandshakeSession:
        self._prune()
        for s in list(self._sessions.values()):
            if s.user_id == user_id and s.active:
                await self.cancel(s.id, user_id)
        wait = self.cooldown_left(user_id)
        if wait > 0:
            mins = max(1, int((wait + 59) // 60))
            raise LoginCooldown(wait, "Pocket Option is limiting sign-ins right now. Please wait about "
                                      f"{mins} minute{'s' if mins != 1 else ''}, then send /at_login again.")
        if sum(1 for s in self._sessions.values() if s.active) >= self._cfg.max_handshakes:
            raise RuntimeError("too many logins in progress, try again shortly")
        session = HandshakeSession(secrets.token_urlsafe(24), user_id, created_at=self._clock.utcnow(),
                                   backend=self._factory(), replace=bool(replace),
                                   last_seen=asyncio.get_running_loop().time())
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
        try:
            s.last_seen = asyncio.get_running_loop().time()
        except RuntimeError:
            pass
        return s

    async def screenshot(self, session_id: str, user_id: str) -> bytes:
        s = self.get(session_id, user_id)
        if not s.active or s.backend is None:
            raise RuntimeError("login session is not active")
        return await asyncio.wait_for(s.backend.screenshot(), timeout=SCREENSHOT_TIMEOUT + 2)

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

    async def shutdown(self) -> None:
        """Service stopping (deploy/restart): close every login browser first."""
        for s in list(self._sessions.values()):
            if s.active or s.backend is not None:
                try:
                    await self.cancel(s.id, s.user_id)
                except Exception:
                    pass

    def _free_mb(self) -> int | None:
        try:
            mem = self._memory()
        except Exception:
            return None
        return int(mem["free_mb"]) if mem else None

    def _report(self, s: HandshakeSession) -> None:
        """Tell the user (Telegram) why the login ended, with what to do next. Never internal details."""
        if self._on_failure is None or s.state not in ("failed", "timeout"):
            return
        try:
            self._on_failure(s.user_id, f"🔐 Pocket Option login: {s.message}")
        except Exception:
            log.info("could not deliver the login failure notice")

    async def _run(self, s: HandshakeSession) -> None:
        bm.install_quiet_playwright_handler()
        watchdog = asyncio.create_task(self._watch_idle(s), name=f"handshake-idle:{s.id[:6]}")
        try:
            await self._run_inner(s)
        finally:
            watchdog.cancel()
            try:
                self._note_result(s, getattr(s, "retry_after", None))
            except Exception:
                pass
            self._report(s)

    async def _watch_idle(self, s: HandshakeSession) -> None:
        """The phone polls every second or two. If it stops (page closed, Telegram minimised for good),
        the login is cancelled so its Chromium does not hold memory until the full login timeout.
        It also watches container memory: if Chromium pushes free memory under the floor, the browser is
        closed by us before the kernel OOM-kills the whole service (and every running trade worker)."""
        idle = float(getattr(self._cfg, "handshake_idle_timeout", IDLE_TIMEOUT) or IDLE_TIMEOUT)
        floor = float(getattr(self._cfg, "handshake_memory_floor_mb", 0) or 0)
        loop = asyncio.get_running_loop()
        while s.active:
            await asyncio.sleep(min(2.0, idle / 3))
            free = self._free_mb() if floor > 0 and s.active else None
            if free is not None and free < floor:
                log.warning("login browser stopped: container memory low (%d MB free, floor %d MB)", free, floor)
                s.error = "low_memory"
                if s.task is not None:
                    s.task.cancel()
                return
            if s.active and s.state != "capturing" and loop.time() - s.last_seen > idle:
                log.info("login page stopped polling; closing its browser")
                s.error = "abandoned"
                if s.task is not None:
                    s.task.cancel()
                return

    async def _space_launch(self, backend: Any) -> None:
        """Real Chromium launches are spaced out server-wide so the broker never sees a burst of sign-ins."""
        if not isinstance(backend, PlaywrightBackend):
            return
        async with self._launch_lock:
            loop = asyncio.get_running_loop()
            if self._last_launch is not None:
                gap = LAUNCH_SPACING - (loop.time() - self._last_launch)
                if gap > 0:
                    await asyncio.sleep(gap)
            self._last_launch = loop.time()

    async def _run_inner(self, s: HandshakeSession) -> None:
        backend = s.backend
        assert backend is not None
        tokens: dict[str, str] = {}
        meta: dict[str, Any] = {}
        if hasattr(backend, "set_challenge_listener"):
            backend.set_challenge_listener(lambda c: setattr(s, "challenge", c))
        try:
            need = float(getattr(self._cfg, "handshake_min_free_mb", 0) or 0)
            if need > 0:
                bm.release_memory()
                free = self._free_mb()
                if free is not None and free < need:
                    log.warning("login browser not started: %d MB free, %d MB needed", free, need)
                    s.state, s.error = "failed", "low_memory"
                    return
            await self._space_launch(backend)
            try:
                await backend.open()
            except asyncio.CancelledError:
                raise
            except BrokerRateLimited:
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
            s.state = "failed" if s.error == "low_memory" else "cancelled"
            return
        except BrokerRateLimited as exc:
            s.state, s.error = "failed", "broker_rate_limited"
            s.retry_after = exc.retry_after
            log.warning("broker rate-limited the sign-in (429)")
            return
        except BrowserCrashed:
            s.state, s.error = "failed", "browser_crashed"
            log.warning("handshake browser crashed (likely out of memory)")
            return
        except ChallengeBlocked as exc:
            s.challenge = exc.challenge
            s.state, s.error = "timeout", CHALLENGE_ERRORS.get(exc.challenge, "login_timeout")
            log.info("login timed out at a %s step", exc.challenge)
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
            meta["replace"] = s.replace
            stored = await self._sink(s.user_id, tokens, meta)
            s.kinds, s.state = sorted(tokens if stored is None else stored), "done"
        except Exception as exc:
            s.state, s.error = "failed", "store_failed"
            log.warning("could not store captured tokens: %s", type(exc).__name__)
        finally:
            tokens.clear()

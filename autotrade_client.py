"""GBot -> autotrade service client (standard library only).

GBot itself still never places trades. These calls only ask the separate autotrade service
(see autotrade/README.md) to start, stop or configure a user's bot. Each Telegram user maps to the
service user id ``tg:<telegram user id>``. Tokens are short-lived HS256 JWTs signed with the shared
AUTOTRADE_JWT_SECRET and are never logged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from runtime_env import load_environment

load_environment()

API_TOKEN_TTL_SECONDS = 120
LOGIN_LINK_TTL_SECONDS = 900          # the /at_login link works for 15 minutes
SIGNAL_TOKEN_TTL_SECONDS = 30         # a signal hand-off token only places that one signal
SERVICE_SUBJECT = "service:gbot"      # GBot itself, for the "who is auto-trading" list
ROSTER_MAX_AGE_SECONDS = 20.0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(secret: str, claims: dict[str, Any]) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def service_user_id(telegram_user_id: int | str) -> str:
    return f"tg:{int(telegram_user_id)}"


class AutotradeError(Exception):
    """A non-2xx answer (status > 0) or no answer at all (status == 0)."""

    def __init__(self, status: int, code: str, message: str = "", kind: str | None = None) -> None:
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message, self.kind = status, code, message, kind


class AutotradeClient:
    def __init__(
        self,
        base_url: str,
        jwt_secret: str,
        *,
        public_url: str | None = None,
        timeout: float = 20.0,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(jwt_secret) < 32:
            raise ValueError("AUTOTRADE_JWT_SECRET must be at least 32 characters")
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self._secret = jwt_secret
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._clock = clock

    @classmethod
    def from_env(cls) -> "AutotradeClient | None":
        """Use the local service by default; no trading commands without a JWT secret."""
        url = os.getenv("AUTOTRADE_API_URL", "").strip() or "http://127.0.0.1:8100"
        secret = os.environ.get("AUTOTRADE_JWT_SECRET", "").strip()
        if not url or not secret:
            return None
        return cls(url, secret, public_url=os.environ.get("AUTOTRADE_PUBLIC_URL", "").strip() or None)

    def token(self, telegram_user_id: int | str | None, *, scope: str = "full", admin: bool = False) -> str:
        now = int(self._clock())
        ttl = {"login": LOGIN_LINK_TTL_SECONDS, "signal": SIGNAL_TOKEN_TTL_SECONDS}.get(scope, API_TOKEN_TTL_SECONDS)
        sub = SERVICE_SUBJECT if scope == "service" else service_user_id(telegram_user_id)  # type: ignore[arg-type]
        claims: dict[str, Any] = {"sub": sub, "iat": now, "exp": now + ttl}
        if scope != "full":
            claims["scope"] = scope
        if admin and scope == "full":
            claims["role"] = "admin"
        return make_jwt(self._secret, claims)

    def login_link(self, telegram_user_id: int | str) -> str:
        # The token goes in the fragment so it is never sent to the server or its logs.
        return f"{self.public_url}/login#t={self.token(telegram_user_id, scope='login')}"

    # -- HTTP ---------------------------------------------------------------------------------
    def _request_sync(self, method: str, path: str, token: str, body: Any, timeout: float) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._error(exc.code, exc.read()) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise AutotradeError(0, "unreachable") from None
        return json.loads(raw) if raw else None

    @staticmethod
    def _error(status: int, raw: bytes) -> AutotradeError:
        try:
            detail = json.loads(raw or b"{}").get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict):
            return AutotradeError(status, str(detail.get("error", status)), str(detail.get("message") or
                                  detail.get("reason") or ""), detail.get("kind"))
        if isinstance(detail, list):   # pydantic validation errors
            text = "; ".join(str(item.get("msg", "")) for item in detail if isinstance(item, dict))
            return AutotradeError(status, "invalid_settings", text)
        return AutotradeError(status, {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(status, "error"),
                              str(detail or ""))

    async def _call(self, method: str, path: str, telegram_user_id: int | str | None, body: Any = None, *,
                    admin: bool = False, timeout: float | None = None, scope: str = "full") -> Any:
        token = self.token(telegram_user_id, admin=admin, scope=scope)
        return await asyncio.to_thread(self._request_sync, method, path, token, body, timeout or self._timeout)

    async def start(self, uid: int | str) -> dict[str, Any]:
        return await self._call("POST", "/trading/start", uid)

    async def stop(self, uid: int | str) -> dict[str, Any]:
        # Waits for open trades to settle (up to ~3 minutes on the service side).
        return await self._call("POST", "/trading/stop", uid, {"drain": True}, timeout=200)

    async def settings(self, uid: int | str) -> dict[str, Any]:
        return await self._call("GET", "/trading/settings", uid)

    async def update_settings(self, uid: int | str, patch: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PATCH", "/trading/settings", uid, patch)

    async def stats(self, uid: int | str) -> dict[str, Any]:
        return await self._call("GET", "/trading/stats", uid)

    async def submit_signal(self, uid: int | str, signal: dict[str, Any]) -> dict[str, Any]:
        """Hand a signal to this user's running auto-trading bot. Uses a short-lived signal-only token."""
        return await self._call("POST", "/trading/signal", uid, signal, timeout=6, scope="signal")

    async def auth_status(self, uid: int | str) -> dict[str, Any]:
        """Saved session state per account type (no broker connection)."""
        return await self._call("GET", "/auth/status", uid)

    async def balance(self, uid: int | str, fresh: bool = True) -> dict[str, Any]:
        return await self._call("GET", f"/trading/balance?fresh={'true' if fresh else 'false'}", uid, timeout=45)

    async def active_users(self) -> set[int]:
        """Telegram ids of users who are auto-trading GBot signals right now."""
        data = await self._call("GET", "/trading/active", None, timeout=5, scope="service")
        ids: set[int] = set()
        for user in (data or {}).get("users", []):
            if isinstance(user, str) and user.startswith("tg:"):
                try:
                    ids.add(int(user[3:]))
                except ValueError:
                    pass
        return ids

    async def submit_manual_session(self, uid: int | str, kind: str, ssid: str) -> dict[str, Any]:
        """Store a pasted SSID in the user's encrypted vault slot. The value is never logged."""
        return await self._call("POST", "/auth/manual-session", uid, {"kind": kind, "ssid": ssid}, timeout=45)

    async def revoke(self, uid: int | str) -> None:
        await self._call("DELETE", "/auth/sessions", uid)

    async def halt(self, admin_uid: int | str) -> None:
        await self._call("POST", "/admin/halt", admin_uid, admin=True)

    async def resume(self, admin_uid: int | str) -> None:
        await self._call("POST", "/admin/resume", admin_uid, admin=True)


class AutotradeRoster:
    """GBot's cached copy of the service's "who is auto-trading" list.

    Signals for chats on this list are handed to the auto-trade service (and their raw cards are not
    sent); everyone else gets the normal card. Local start/stop updates apply at once; the list is
    re-read from the service when older than ``max_age`` seconds. If the service cannot be reached,
    the last known list is kept, so nobody is switched by a network hiccup."""

    def __init__(self, client: AutotradeClient | None, *, max_age: float = ROSTER_MAX_AGE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.max_age, self._clock = client, max_age, clock
        self._ids: set[int] = set()
        self._added_at: dict[int, float] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    def active_ids(self) -> set[int]:
        return set(self._ids)

    def is_active(self, chat_id: int) -> bool:
        return int(chat_id) in self._ids

    def add(self, chat_id: int) -> None:
        self._ids.add(int(chat_id))
        self._added_at[int(chat_id)] = self._clock()

    def release(self, chat_id: int, grace: float = 90.0) -> None:
        """Discard unless the chat started auto-trading within ``grace`` seconds (worker still starting)."""
        started = self._added_at.get(int(chat_id))
        if started is not None and self._clock() - started < grace:
            return
        self.discard(chat_id)

    def discard(self, chat_id: int) -> None:
        self._ids.discard(int(chat_id))

    @property
    def stale(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at > self.max_age

    async def refresh(self, timeout: float = 3.0) -> bool:
        if self.client is None:
            return False
        async with self._lock:
            if not self.stale:
                return True
            try:
                ids = await asyncio.wait_for(self.client.active_users(), timeout=timeout)
            except Exception:
                self._fetched_at = self._clock() - self.max_age / 2    # retry soon, keep the old list
                return False
            self._ids, self._fetched_at = ids, self._clock()
            return True

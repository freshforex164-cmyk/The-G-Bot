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

API_TOKEN_TTL_SECONDS = 120
LOGIN_LINK_TTL_SECONDS = 900          # the /at_login link works for 15 minutes


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
        """None (auto-trading commands answer "not set up") unless both variables are set."""
        url = os.environ.get("AUTOTRADE_API_URL", "").strip()
        secret = os.environ.get("AUTOTRADE_JWT_SECRET", "").strip()
        if not url or not secret:
            return None
        return cls(url, secret, public_url=os.environ.get("AUTOTRADE_PUBLIC_URL", "").strip() or None)

    def token(self, telegram_user_id: int | str, *, scope: str = "full", admin: bool = False) -> str:
        now = int(self._clock())
        ttl = LOGIN_LINK_TTL_SECONDS if scope == "login" else API_TOKEN_TTL_SECONDS
        claims: dict[str, Any] = {"sub": service_user_id(telegram_user_id), "iat": now, "exp": now + ttl}
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

    async def _call(self, method: str, path: str, telegram_user_id: int | str, body: Any = None, *,
                    admin: bool = False, timeout: float | None = None) -> Any:
        token = self.token(telegram_user_id, admin=admin)
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

    async def revoke(self, uid: int | str) -> None:
        await self._call("DELETE", "/auth/sessions", uid)

    async def halt(self, admin_uid: int | str) -> None:
        await self._call("POST", "/admin/halt", admin_uid, admin=True)

    async def resume(self, admin_uid: int | str) -> None:
        await self._call("POST", "/admin/resume", admin_uid, admin=True)

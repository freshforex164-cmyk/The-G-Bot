from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..auth.browser_memory import container_memory, install_quiet_playwright_handler
from ..auth.handshake import LoginCooldown
from ..errors import ReauthRequired, SettingsError
from .ratelimit import RateLimiter, parse_rate, wait_text
from ..auth.ssid import is_well_formed
from ..runtime import Runtime
from ..trading.manager import CapacityError, LiveDisabled, ServiceUnavailable, TradingHalted
from ..trading.models import Decision

log = logging.getLogger("autotrade.api")


@dataclass(frozen=True)
class Principal:
    user_id: str
    is_admin: bool = False
    # "full" = normal API token. "login" = short-lived link token (e.g. sent by the Telegram bot)
    # that may only drive /auth/handshake*. "signal" = GBot's signal hand-off token: only
    # POST /trading/signal. "service" = GBot itself (sub "service:gbot"): only GET /trading/active.
    # Every route rejects the scopes it was not made for.
    scope: str = "full"


SCOPES = ("full", "login", "signal", "service")
SERVICE_SUBJECT = "service:gbot"
TOO_MANY_LOGINS = "Another sign-in is in progress on the server. Try again in a minute."
SUBMIT_FAILED = "The sign-in could not be submitted. Use the Pocket Option page to continue."
SAFE_SUBMIT_MESSAGES = frozenset({
    "The broker login page is not ready.",
    "Use the broker page to complete this login.",
    "Could not submit the broker login. Use the broker page to continue.",
    "Enter a valid email address.",
    "Enter a valid password.",
})
LOGIN_PAGE = Path(__file__).with_name("login_page.html")


def jwt_principal(secret: str, audience: str | None = None) -> Callable[..., Principal]:
    """Default auth: your existing system issues an HS256 JWT with ``sub`` = internal user id.
    Replace with your own dependency via ``create_app(auth=...)`` if you already have sessions."""
    if len(secret) < 32:
        raise RuntimeError("AUTOTRADE_JWT_SECRET must be at least 32 characters")
    bearer = HTTPBearer(auto_error=False)

    def dep(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = jwt.decode(creds.credentials, secret, algorithms=["HS256"], audience=audience,
                                options={"require": ["exp", "sub"], "verify_aud": audience is not None})
        except jwt.PyJWTError:
            raise HTTPException(401, "invalid or expired token") from None
        sub = str(claims["sub"])
        if not (0 < len(sub) <= 128):
            raise HTTPException(401, "invalid token")
        scope = str(claims.get("scope", "full"))
        if scope not in SCOPES:
            raise HTTPException(401, "invalid token")
        return Principal(sub, scope == "full" and claims.get("role") == "admin", scope)

    return dep


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["demo", "live"] | None = None
    confirm_live: bool = False
    strategy: str | None = Field(None, max_length=32)
    symbol_real: str | None = Field(None, max_length=32)
    symbol_otc: str | None = Field(None, max_length=32)
    stake: float | None = None
    expiry_seconds: int | None = None
    max_concurrent: int | None = None
    daily_stop_loss: float | None = None
    daily_take_profit: float | None = None
    stop_balance: float | None = None
    cooldown_seconds: int | None = None
    min_payout: int | None = None
    max_tick_age_seconds: float | None = None
    follow_signals: bool | None = None
    stake_mode: Literal["fixed", "martingale"] | None = None
    martingale_multiplier: float | None = None
    martingale_max_steps: int | None = None
    base_stake: float | None = None
    route_assets: list[str] | None = Field(None, max_length=4)


class SignalIn(BaseModel):
    """A signal GBot just delivered to this user in Telegram."""
    model_config = ConfigDict(extra="forbid")
    signal_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    asset: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_#-]+$")
    direction: Literal["BUY", "SELL", "CALL", "PUT"]
    expiry_seconds: Literal[60] = 60
    setup: str = Field("", max_length=48)
    asset_name: str = Field("", max_length=48)       # display name for trade alerts, e.g. "EUR/USD OTC"
    valid_until: datetime | None = None


class HandshakeStart(BaseModel):
    """The phone's own screen size (CSS px), so the site is rendered to fit it."""
    model_config = ConfigDict(extra="forbid")
    # Any size is accepted and clamped to a phone layout by the browser relay: a Telegram window that is
    # still half-open (small height) must not turn into an error page.
    width: int | None = Field(None, ge=0, le=10000)
    height: int | None = Field(None, ge=0, le=10000)
    full_view: bool = False     # the user will sign in on the broker page itself (styles and pictures load)
    force: bool = False         # sign in again even though a saved session still works


class ManualSession(BaseModel):
    """A Pocket Option SSID the user pasted in Telegram (deleted from the chat by GBot)."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["demo", "real"]
    ssid: SecretStr = Field(min_length=20, max_length=4096)


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drain: bool = True


class BrokerCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value):
        return value.strip() if isinstance(value, str) else value


class InputEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click", "value", "text", "key", "scroll"]
    x: int | None = Field(None, ge=0, le=4000)
    y: int | None = Field(None, ge=0, le=4000)
    text: str | None = Field(None, max_length=256)
    value: str | None = Field(None, max_length=256)   # "value": whole text of the focused field (may be "")
    key: str | None = Field(None, max_length=16)
    dy: int | None = Field(None, ge=-5000, le=5000)


def is_secure_request(request: Request, trust_proxy: bool) -> bool:
    """True for HTTPS, including TLS terminated by the configured local TLS proxy."""
    if request.url.scheme == "https":
        return True
    if trust_proxy:
        h = request.headers
        proto = h.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        forwarded = h.get("forwarded", "").split(",")[0].lower().replace(" ", "")
        if proto == "https" or "proto=https" in forwarded or h.get("x-forwarded-ssl", "").lower() == "on" \
                or '"scheme":"https"' in h.get("cf-visitor", "").replace(" ", ""):
            return True
    return request.client is not None and request.client.host in {"127.0.0.1", "::1", "testclient"}


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, ReauthRequired):
        return HTTPException(409, {"error": "reauth_required", "kind": exc.kind, "reason": exc.reason,
                                   "action": "POST /auth/handshake"})
    if isinstance(exc, LiveDisabled):
        return HTTPException(403, {"error": "live_disabled", "message": str(exc)})
    if isinstance(exc, SettingsError):
        return HTTPException(422, {"error": "invalid_settings", "message": str(exc)})
    if isinstance(exc, (TradingHalted, CapacityError, ServiceUnavailable)):
        return HTTPException(503, {"error": type(exc).__name__, "message": str(exc)})
    raise exc


def create_app(rt: Runtime, auth: Callable[..., Principal] | None = None, *,
               before_start: Callable[[], Any] | None = None, after_stop: Callable[[], Any] | None = None,
               background: bool = True) -> FastAPI:
    auth = auth or jwt_principal(rt.cfg.jwt_secret, rt.cfg.jwt_audience)

    def full(p: Principal = Depends(auth)) -> Principal:
        if getattr(p, "scope", "full") != "full":
            raise HTTPException(403, "this token cannot be used here")
        return p

    def login(p: Principal = Depends(auth)) -> Principal:
        if getattr(p, "scope", "full") not in ("full", "login"):
            raise HTTPException(403, "this token cannot be used here")
        return p

    def signal_token(p: Principal = Depends(auth)) -> Principal:
        if getattr(p, "scope", "full") != "signal":
            raise HTTPException(403, "signal hand-offs need a signal token")
        return p

    def service_token(p: Principal = Depends(auth)) -> Principal:
        if getattr(p, "scope", "full") != "service" or p.user_id != SERVICE_SUBJECT:
            raise HTTPException(403, "service token required")
        return p

    handshake_limit = RateLimiter(*parse_rate(rt.cfg.handshake_rate, (6, 600)))
    credentials_limit = RateLimiter(*parse_rate(rt.cfg.credentials_rate, (5, 900)))

    def limited(limiter: RateLimiter, user_id: str) -> None:
        wait = limiter.hit(user_id)
        if wait:
            raise HTTPException(429, {"error": "rate_limited", "message": wait_text(wait),
                                      "retry_after": int(wait)}, headers={"Retry-After": str(int(wait))})

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        install_quiet_playwright_handler()
        if before_start:
            await before_start()
        if not background:
            try:
                yield
            finally:
                await rt.handshakes.shutdown()
                await rt.manager.shutdown()
                if after_stop:
                    await after_stop()
            return
        health_task = asyncio.create_task(rt.health.run_forever(
            600, rt.cfg.recheck_seconds, skip=rt.manager.is_running))
        await rt.manager.resume_all()
        try:
            yield
        finally:
            health_task.cancel()
            await rt.handshakes.shutdown()      # close login browsers before anything else
            await rt.manager.shutdown()
            if after_stop:
                await after_stop()

    app = FastAPI(title="Autotrade service", lifespan=lifespan, docs_url=None, redoc_url=None)
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # FastAPI's default errors include the rejected input, which may contain a password/token.
        return JSONResponse(status_code=422, content={"detail": [
            {"type": error["type"], "loc": list(error["loc"]), "msg": "Invalid input."}
            for error in exc.errors()
        ]})
    if rt.cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(rt.cfg.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith(("/login", "/auth/handshake")):
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "feed": rt.feed.status(), "halted": rt.manager.halted,
                "memory": container_memory()}       # MB numbers only: shows how close the 512 MB cap is

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        """Phone login page (opened from the Telegram bot or your frontend): shows the real Pocket Option
        mobile site edge to edge and puts a native input over the focused field. The token
        travels in the URL fragment (#t=...), so it never reaches server or proxy logs."""
        nonce = secrets.token_urlsafe(16)
        html = LOGIN_PAGE.read_text(encoding="utf-8").replace("{{NONCE}}", nonce)
        return HTMLResponse(html, headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}' https://telegram.org; "
                "connect-src 'self'; img-src blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"),
            "Referrer-Policy": "no-referrer",
        })

    # ---- trading -------------------------------------------------------------------------
    @app.post("/trading/start")
    async def start(p: Principal = Depends(full)):
        try:
            return await rt.manager.start(p.user_id)
        except Exception as exc:
            raise _http(exc) from None

    @app.post("/trading/stop")
    async def stop(body: StopBody | None = None, p: Principal = Depends(full)):
        return await rt.manager.stop(p.user_id, drain=(body.drain if body else True))

    @app.get("/trading/settings")
    async def get_settings(p: Principal = Depends(full)):
        return (await rt.repo.get_settings(p.user_id)).to_dict()

    @app.patch("/trading/settings")
    async def patch_settings(body: SettingsPatch, p: Principal = Depends(full)):
        patch = body.model_dump(exclude_unset=True, exclude={"confirm_live"})
        try:
            return (await rt.manager.update_settings(p.user_id, patch, confirm_live=body.confirm_live)).to_dict()
        except Exception as exc:
            raise _http(exc) from None

    @app.get("/trading/stats")
    async def stats(p: Principal = Depends(full)):
        return await rt.manager.stats(p.user_id)

    @app.post("/trading/signal")
    async def signal(body: SignalIn, p: Principal = Depends(signal_token)):
        """GBot hands each signal for an auto-trading user to their running bot (risk rules apply)."""
        direction = "CALL" if body.direction in ("BUY", "CALL") else "PUT"
        meta = {"asset_name": body.asset_name} if body.asset_name else {}
        decision = Decision(asset=body.asset, direction=direction, expiry_seconds=60,
                            reason=body.setup or "gbot_signal", signal_id=body.signal_id,
                            valid_until=body.valid_until, meta=meta)
        return await rt.manager.submit_signal(p.user_id, decision)

    @app.get("/trading/active")
    async def active(p: Principal = Depends(service_token)):
        """Who is auto-trading right now (running bots that place GBot signals)."""
        return {"users": rt.manager.active_users()}

    @app.get("/trading/balance")
    async def balance(fresh: bool = True, p: Principal = Depends(full)):
        """Account balance for the selected mode; works while the bot is stopped (last value is saved)."""
        return await rt.manager.balance(p.user_id, fresh=fresh)

    # ---- session handshake ---------------------------------------------------------------
    @app.get("/auth/status")
    async def auth_status(p: Principal = Depends(full)):
        return await rt.vault.status(p.user_id)

    @app.post("/auth/handshake", status_code=201)
    async def handshake_start(body: HandshakeStart | None = None, p: Principal = Depends(login)):
        limited(handshake_limit, p.user_id)
        if not (body and body.force):
            # Reuse the saved session: every extra browser login from the server's IP risks a broker block.
            try:
                reused = await rt.manager.reusable_session(p.user_id)
            except Exception as exc:
                log.warning("saved-session check failed: %s", type(exc).__name__)
                reused = None
            if reused:
                return JSONResponse({"reused": True, "state": "done", **reused}, status_code=200)
        vp = (body.width, body.height) if body and body.width and body.height else None
        try:
            s = await rt.handshakes.start(p.user_id, vp, full_view=bool(body and body.full_view),
                                          replace=bool(body and body.force))
        except LoginCooldown as exc:
            raise HTTPException(429, {"error": "broker_cooldown", "message": exc.message,
                                      "retry_after": int(exc.wait) + 1},
                                headers={"Retry-After": str(int(exc.wait) + 1)}) from None
        except RuntimeError:
            raise HTTPException(503, {"error": "busy", "message": TOO_MANY_LOGINS}) from None
        except Exception as exc:     # never a blank 500 page
            log.warning("could not start the sign-in browser: %s", type(exc).__name__)
            raise HTTPException(503, {"error": "browser_unavailable",
                                      "message": "The secure sign-in browser could not start. Try again in a minute."}) from None
        return {"session_id": s.id, "state": s.state}

    def _get(session_id: str, p: Principal):
        try:
            return rt.handshakes.get(session_id, p.user_id)
        except KeyError:
            raise HTTPException(404, "unknown login session") from None

    @app.get("/auth/handshake/{session_id}")
    async def handshake_status(session_id: str, p: Principal = Depends(login)):
        s = _get(session_id, p)
        return {"session_id": s.id, "state": s.state, "error": s.error, "message": s.message, "kinds": s.kinds,
                "challenge": s.challenge, "hint": s.hint}

    @app.get("/auth/handshake/{session_id}/screen")
    async def handshake_screen(session_id: str, p: Principal = Depends(login)):
        if _get(session_id, p).state == "starting":
            return Response(status_code=204)      # browser still opening: the page keeps its loading panel
        try:
            return Response(await rt.handshakes.screenshot(session_id, p.user_id), media_type="image/jpeg")
        except Exception:       # not active, navigating or the browser just closed
            raise HTTPException(409, "login session is not active") from None

    @app.get("/auth/handshake/{session_id}/view")
    async def handshake_view(session_id: str, p: Principal = Depends(login)):
        """Phone viewport size + the focused input's position/type, so the page can place a native
        input over it. Password values are never returned."""
        if _get(session_id, p).state == "starting":
            return Response(status_code=204)
        try:
            return await rt.handshakes.view(session_id, p.user_id)
        except Exception:
            raise HTTPException(409, "login session is not active") from None

    @app.post("/auth/handshake/{session_id}/input", status_code=204)
    async def handshake_input(session_id: str, ev: InputEvent, p: Principal = Depends(login)):
        _get(session_id, p)
        if (ev.type == "click" and (ev.x is None or ev.y is None)) or (ev.type == "text" and not ev.text) \
                or (ev.type == "key" and not ev.key) or (ev.type == "value" and ev.value is None):
            raise HTTPException(422, "missing fields for this input type")
        try:
            await rt.handshakes.interact(session_id, p.user_id, ev.model_dump(exclude_none=True))
        except Exception:
            raise HTTPException(409, "login session is not active") from None
        return Response(status_code=204)

    @app.post("/auth/handshake/{session_id}/credentials", status_code=204)
    async def handshake_credentials(session_id: str, body: BrokerCredentials, request: Request,
                                    p: Principal = Depends(login)):
        _get(session_id, p)
        try:
            limited(credentials_limit, p.user_id)
        except HTTPException:
            body.password = SecretStr("")
            raise
        # TLS is mandatory outside local tests. Uvicorn accepts forwarded protocol only
        # from configured proxy IPs; raw forwarded headers are not trusted by default.
        if not is_secure_request(request, rt.cfg.trust_proxy):
            body.password = SecretStr("")
            raise HTTPException(400, {"error": "https_required", "message": "A secure HTTPS connection is required."})
        try:
            await rt.handshakes.submit_credentials(session_id, p.user_id, body.email, body.password.get_secret_value())
        except RuntimeError as exc:
            # Only our own fixed texts are shown; anything else could echo user input.
            text = str(exc) if str(exc) in SAFE_SUBMIT_MESSAGES else SUBMIT_FAILED
            raise HTTPException(409, {"error": "submit_failed", "message": text}) from None
        except Exception as exc:   # browser crashed/navigated: never a bare 500 page
            log.warning("credential submission failed: %s", type(exc).__name__)
            raise HTTPException(409, {"error": "submit_failed", "message": SUBMIT_FAILED}) from None
        finally:
            body.password = SecretStr("")
        return Response(status_code=204)

    @app.delete("/auth/handshake/{session_id}", status_code=204)
    async def handshake_cancel(session_id: str, p: Principal = Depends(login)):
        _get(session_id, p)
        await rt.handshakes.cancel(session_id, p.user_id)
        return Response(status_code=204)

    @app.post("/auth/manual-session")
    async def manual_session(body: ManualSession, p: Principal = Depends(full)):
        """Save a pasted SSID into the encrypted vault slot it belongs to (Demo and Real never mix)."""
        limited(handshake_limit, p.user_id)
        raw = body.ssid.get_secret_value().strip()
        body.ssid = SecretStr("")
        if not is_well_formed(raw, body.kind):
            raise HTTPException(422, {"error": "invalid_ssid",
                                      "message": f"That is not a valid {body.kind} Pocket Option session."})
        await rt.vault.store(p.user_id, body.kind, raw, {"method": "manual", "derived": False})
        raw = ""
        await rt.repo.save_settings(p.user_id, await rt.repo.get_settings(p.user_id))
        await rt.repo.audit(p.user_id, "tokens_captured", {"kinds": [body.kind], "method": "manual"})
        connected = None
        try:
            await rt.manager.connect_new_sessions(p.user_id, [body.kind])
            connected = True
        except Exception as exc:
            log.warning("manual session connection check failed: %s", type(exc).__name__)
            connected = False
        return {"stored": body.kind, "connected": connected}

    @app.delete("/auth/sessions", status_code=204)
    async def disconnect(p: Principal = Depends(full)):
        """User-initiated revoke: stop the bot and erase stored tokens."""
        await rt.manager.stop(p.user_id, drain=False)
        await rt.vault.delete_all(p.user_id)
        await rt.manager.reset_to_demo(p.user_id, "tokens deleted")
        await rt.repo.audit(p.user_id, "tokens_deleted", {})
        return Response(status_code=204)

    # ---- operator kill switch ------------------------------------------------------------
    @app.post("/admin/halt", status_code=204)
    async def halt(p: Principal = Depends(full)):
        if not p.is_admin:
            raise HTTPException(403, "admin only")
        rt.manager.halted = True
        await rt.repo.audit(p.user_id, "operator_halt", {})
        return Response(status_code=204)

    @app.post("/admin/resume", status_code=204)
    async def resume(p: Principal = Depends(full)):
        if not p.is_admin:
            raise HTTPException(403, "admin only")
        rt.manager.halted = False
        await rt.repo.audit(p.user_id, "operator_resume", {})
        return Response(status_code=204)

    return app

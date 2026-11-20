from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from ..errors import ReauthRequired, SettingsError
from ..runtime import Runtime
from ..trading.manager import CapacityError, LiveDisabled, ServiceUnavailable, TradingHalted

log = logging.getLogger("autotrade.api")


@dataclass(frozen=True)
class Principal:
    user_id: str
    is_admin: bool = False


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
        return Principal(sub, claims.get("role") == "admin")

    return dep


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["demo", "live"] | None = None
    confirm_live: bool = False
    strategy: str | None = Field(None, max_length=32)
    symbol_real: str | None = Field(None, max_length=32)
    symbol_otc: str | None = Field(None, max_length=32)
    gap_threshold: float | None = None
    stake: float | None = None
    expiry_seconds: int | None = None
    max_concurrent: int | None = None
    daily_stop_loss: float | None = None
    daily_take_profit: float | None = None
    cooldown_seconds: int | None = None
    min_payout: int | None = None
    max_tick_age_seconds: float | None = None


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drain: bool = True


class InputEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click", "text", "key", "scroll"]
    x: int | None = Field(None, ge=0, le=4000)
    y: int | None = Field(None, ge=0, le=4000)
    text: str | None = Field(None, max_length=256)
    key: str | None = Field(None, max_length=16)
    dy: int | None = Field(None, ge=-5000, le=5000)


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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if before_start:
            await before_start()
        if not background:
            try:
                yield
            finally:
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
            await rt.manager.shutdown()
            if after_stop:
                await after_stop()

    app = FastAPI(title="Autotrade service", lifespan=lifespan, docs_url=None, redoc_url=None)
    if rt.cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(rt.cfg.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "feed": rt.feed.status(), "halted": rt.manager.halted}

    # ---- trading -------------------------------------------------------------------------
    @app.post("/trading/start")
    async def start(p: Principal = Depends(auth)):
        try:
            return await rt.manager.start(p.user_id)
        except Exception as exc:
            raise _http(exc) from None

    @app.post("/trading/stop")
    async def stop(body: StopBody | None = None, p: Principal = Depends(auth)):
        return await rt.manager.stop(p.user_id, drain=(body.drain if body else True))

    @app.get("/trading/settings")
    async def get_settings(p: Principal = Depends(auth)):
        return (await rt.repo.get_settings(p.user_id)).to_dict()

    @app.patch("/trading/settings")
    async def patch_settings(body: SettingsPatch, p: Principal = Depends(auth)):
        patch = body.model_dump(exclude_unset=True, exclude={"confirm_live"})
        try:
            return (await rt.manager.update_settings(p.user_id, patch, confirm_live=body.confirm_live)).to_dict()
        except Exception as exc:
            raise _http(exc) from None

    @app.get("/trading/stats")
    async def stats(p: Principal = Depends(auth)):
        return await rt.manager.stats(p.user_id)

    # ---- session handshake ---------------------------------------------------------------
    @app.get("/auth/status")
    async def auth_status(p: Principal = Depends(auth)):
        return await rt.vault.status(p.user_id)

    @app.post("/auth/handshake", status_code=201)
    async def handshake_start(p: Principal = Depends(auth)):
        try:
            s = await rt.handshakes.start(p.user_id)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None
        return {"session_id": s.id, "state": s.state}

    def _get(session_id: str, p: Principal):
        try:
            return rt.handshakes.get(session_id, p.user_id)
        except KeyError:
            raise HTTPException(404, "unknown login session") from None

    @app.get("/auth/handshake/{session_id}")
    async def handshake_status(session_id: str, p: Principal = Depends(auth)):
        s = _get(session_id, p)
        return {"session_id": s.id, "state": s.state, "error": s.error, "kinds": s.kinds}

    @app.get("/auth/handshake/{session_id}/screen")
    async def handshake_screen(session_id: str, p: Principal = Depends(auth)):
        _get(session_id, p)
        try:
            return Response(await rt.handshakes.screenshot(session_id, p.user_id), media_type="image/jpeg")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/auth/handshake/{session_id}/input", status_code=204)
    async def handshake_input(session_id: str, ev: InputEvent, p: Principal = Depends(auth)):
        _get(session_id, p)
        if (ev.type == "click" and (ev.x is None or ev.y is None)) or (ev.type == "text" and not ev.text) \
                or (ev.type == "key" and not ev.key):
            raise HTTPException(422, "missing fields for this input type")
        try:
            await rt.handshakes.interact(session_id, p.user_id, ev.model_dump(exclude_none=True))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    @app.delete("/auth/handshake/{session_id}", status_code=204)
    async def handshake_cancel(session_id: str, p: Principal = Depends(auth)):
        _get(session_id, p)
        await rt.handshakes.cancel(session_id, p.user_id)
        return Response(status_code=204)

    @app.delete("/auth/sessions", status_code=204)
    async def disconnect(p: Principal = Depends(auth)):
        """User-initiated revoke: stop the bot and erase stored tokens."""
        await rt.manager.stop(p.user_id, drain=False)
        await rt.vault.delete_all(p.user_id)
        await rt.manager.reset_to_demo(p.user_id, "tokens deleted")
        await rt.repo.audit(p.user_id, "tokens_deleted", {})
        return Response(status_code=204)

    # ---- operator kill switch ------------------------------------------------------------
    @app.post("/admin/halt", status_code=204)
    async def halt(p: Principal = Depends(auth)):
        if not p.is_admin:
            raise HTTPException(403, "admin only")
        rt.manager.halted = True
        await rt.repo.audit(p.user_id, "operator_halt", {})
        return Response(status_code=204)

    @app.post("/admin/resume", status_code=204)
    async def resume(p: Principal = Depends(auth)):
        if not p.is_admin:
            raise HTTPException(403, "admin only")
        rt.manager.halted = False
        await rt.repo.audit(p.user_id, "operator_resume", {})
        return Response(status_code=204)

    return app

"""Broker abstraction. Workers and the feed only talk to ``BrokerClient`` so they can be tested
offline; ``PocketOptionBroker`` is the BinaryOptionsTools-v2 adapter."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from ..errors import BrokerError, Disconnected, RateLimited, SessionExpired
from ..logging_utils import redact

log = logging.getLogger("autotrade.broker")


@dataclass(frozen=True)
class TradeOutcome:
    result: str            # win | loss | draw | unknown
    profit: float | None


class BrokerClient(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def is_demo(self) -> bool: ...
    async def is_alive(self) -> bool: ...
    async def payout(self, asset: str) -> int | None: ...
    async def balance(self) -> float | None: ...
    async def place(self, asset: str, direction: str, amount: float, expiry_seconds: int) -> str: ...
    async def outcome(self, trade_id: str, timeout: float) -> TradeOutcome: ...
    def ticks(self, symbol: str) -> AsyncIterator[float | dict]: ...


_EXPIRED = ("ssid", "session", "unauthor", "auth", "not authenticated", "forbidden", "invalid token")
_RATE = ("rate limit", "too many", "429", "throttl")
_DISCONNECT = ("connection", "websocket", "closed", "channel", "timed out", "timeout", "reset", "broken pipe", "eof")


def map_exception(exc: BaseException) -> Exception:
    """Translate library errors into typed errors. Matching is by message because the library
    raises generic types; verify against real failures before relying on a new branch."""
    if isinstance(exc, (SessionExpired, RateLimited, Disconnected, BrokerError)):
        return exc
    text = redact(f"{type(exc).__name__}: {exc}").lower()
    name = type(exc).__name__
    if any(k in text for k in _RATE):
        return RateLimited(redact(str(exc))[:200])
    if name in {"NotAllowedError", "InvalidParameterError", "TradeNotFoundError"}:
        return BrokerError(redact(str(exc))[:200])
    if any(k in text for k in _EXPIRED) and not any(k in text for k in ("timeout", "timed out")):
        return SessionExpired(redact(str(exc))[:200])
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError)) or any(k in text for k in _DISCONNECT):
        return Disconnected(redact(str(exc))[:200])
    return BrokerError(redact(f"{name}: {exc}")[:200])


class PocketOptionBroker:
    def __init__(self, ssid: str, *, connect_timeout: float = 30.0):
        self._ssid = ssid
        self._timeout = connect_timeout
        self._api = None

    async def connect(self) -> None:
        from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

        try:
            self._api = PocketOptionAsync(self._ssid)
        except ValueError as exc:
            raise SessionExpired("ssid is malformed") from exc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while loop.time() < deadline:
            if self._api.is_ssid_valid():
                return
            client = getattr(self._api, "client", None)
            try:
                rejected = bool(client.auth_error()) if client is not None else False
            except Exception:
                rejected = False
            if rejected:
                await self.close()
                raise SessionExpired("broker rejected the session")
            await asyncio.sleep(0.25)
        await self.close()
        raise Disconnected("broker did not confirm the session in time")

    async def close(self) -> None:
        api, self._api = self._api, None
        if api is not None:
            try:
                await api.shutdown()
            except Exception:  # best effort
                log.debug("shutdown failed", exc_info=False)

    def _need(self):
        if self._api is None:
            raise Disconnected("not connected")
        return self._api

    async def is_demo(self) -> bool:
        try:
            value = self._need().is_demo()          # sync in 0.2.18; tolerate an async version
            if asyncio.iscoroutine(value):
                value = await value
            if value in (0, "0"):
                return False
            if value in (1, "1"):
                return True
            raise BrokerError("broker account mode is unavailable")
        except Exception as exc:
            raise map_exception(exc) from None

    async def is_alive(self) -> bool:
        api = self._api
        return bool(api is not None and api.is_responsive())

    async def balance(self) -> float | None:
        """Balance of the connected account (Demo or Real, whichever this SSID belongs to)."""
        try:
            value = self._need().balance()
            if asyncio.iscoroutine(value):
                value = await value
            return float(value) if value is not None else None
        except Exception as exc:
            raise map_exception(exc) from None

    async def payout(self, asset: str) -> int | None:
        try:
            return await self._need().payout(asset, timeout=5.0)
        except Exception as exc:
            raise map_exception(exc) from None

    async def place(self, asset: str, direction: str, amount: float, expiry_seconds: int) -> str:
        api = self._need()
        try:
            fn = api.buy if direction == "CALL" else api.sell
            trade_id, _ = await asyncio.wait_for(
                fn(asset=asset, amount=amount, time=expiry_seconds, check_win=False), timeout=20.0)
            return str(trade_id)
        except Exception as exc:
            raise map_exception(exc) from None

    async def outcome(self, trade_id: str, timeout: float) -> TradeOutcome:
        try:
            trade = await self._need().check_win(trade_id, timeout_seconds=int(timeout))
        except Exception as exc:
            raise map_exception(exc) from None
        result = str(trade.get("result", "unknown")).lower()
        profit = trade.get("profit")
        return TradeOutcome(result if result in {"win", "loss", "draw"} else "unknown",
                            float(profit) if profit is not None else None)

    async def ticks(self, symbol: str) -> AsyncIterator[float | dict]:
        try:
            sub = await self._need().subscribe_symbol(symbol)
            async for candle in sub:
                yield {"price": float(candle["close"]), "timestamp": candle.get("timestamp", candle.get("time"))}
        except Exception as exc:
            raise map_exception(exc) from None

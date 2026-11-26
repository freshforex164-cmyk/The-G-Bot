"""One shared price listener. Workers register interest in symbols; each symbol is streamed once
no matter how many users need it, and every tick wakes all registered workers."""
from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from typing import Callable

from ..clock import Clock
from ..errors import SessionExpired
from ..trading.broker import BrokerClient
from ..trading.models import Tick

log = logging.getLogger("autotrade.feed")


class SharedMarketFeed:
    def __init__(self, broker_factory: Callable[[], BrokerClient | None], clock: Clock | None = None,
                 *, backoff_max: float = 60.0):
        self._factory = broker_factory
        self._clock = clock or Clock()
        self._refs: Counter[str] = Counter()
        self._ticks: dict[str, Tick] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._listeners: set[asyncio.Event] = set()
        self._broker: BrokerClient | None = None
        self._lock = asyncio.Lock()
        self._backoff_max = backoff_max
        self.state = "idle"       # idle | connected | unconfigured | rejected | reconnecting
        self.stream_starts: Counter[str] = Counter()   # for tests/monitoring
        self._payouts: dict[str, tuple[int, float]] = {}  # symbol -> (payout %, read at), short-lived
        self.payout_ttl = 5.0

    # -- public API ---------------------------------------------------------------------------
    async def acquire(self, symbols: tuple[str, ...]) -> None:
        for sym in set(symbols):
            self._refs[sym] += 1
            if self._refs[sym] == 1:
                self._tasks[sym] = asyncio.create_task(self._run(sym), name=f"feed:{sym}")

    async def release(self, symbols: tuple[str, ...]) -> None:
        cancelled = []
        for sym in set(symbols):
            if self._refs[sym] <= 0:
                continue
            self._refs[sym] -= 1
            if self._refs[sym] == 0:
                del self._refs[sym]
                task = self._tasks.pop(sym, None)
                if task:
                    task.cancel()
                    cancelled.append(task)
                self._ticks.pop(sym, None)
        await asyncio.gather(*cancelled, return_exceptions=True)
        if not self._refs:              # nobody needs prices: close the idle WebSocket too
            await self._drop_broker()

    async def payouts(self, symbols: list[str] | tuple[str, ...]) -> dict[str, int]:
        """Live payouts over the shared feed connection, cached for a few seconds so many users evaluating
        the same assets cause one broker request. Never opens a socket: empty when the feed is idle."""
        now = self._clock.monotonic()
        wanted = list(dict.fromkeys(symbols))
        out: dict[str, int] = {}
        for s in wanted:
            hit = self._payouts.get(s)
            if hit is not None and now - hit[1] < self.payout_ttl:
                out[s] = hit[0]
        missing = [s for s in wanted if s not in out]
        broker = self._broker
        if missing and broker is not None:
            try:
                fn = getattr(broker, "payouts", None)
                if callable(fn):
                    got = await asyncio.wait_for(fn(missing), timeout=6.0)
                else:
                    got = {}
                    for s in missing:
                        got[s] = await asyncio.wait_for(broker.payout(s), timeout=6.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("feed payouts unavailable (%s)", type(exc).__name__)
                got = {}
            for s, p in got.items():
                if p is not None:
                    self._payouts[s] = (int(p), now)
                    out[s] = int(p)
        if len(self._payouts) > 256:                       # bounded cache
            for s, (_, at) in list(self._payouts.items()):
                if now - at >= self.payout_ttl:
                    del self._payouts[s]
        return out

    def latest(self, symbol: str) -> Tick | None:
        return self._ticks.get(symbol)

    def age(self, symbol: str) -> float:
        t = self._ticks.get(symbol)
        return float("inf") if t is None else self._clock.monotonic() - t.received_at

    def register(self) -> asyncio.Event:
        ev = asyncio.Event()
        self._listeners.add(ev)
        return ev

    def unregister(self, ev: asyncio.Event) -> None:
        self._listeners.discard(ev)

    def status(self) -> dict:
        return {"state": self.state, "symbols": sorted(self._refs), "listeners": len(self._listeners)}

    async def aclose(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._refs.clear()
        self._ticks.clear()
        self._listeners.clear()
        self._payouts.clear()
        await self._drop_broker()

    # -- internals ----------------------------------------------------------------------------
    async def _get_broker(self) -> BrokerClient | None:
        async with self._lock:
            if self._broker is not None:
                return self._broker
            broker = self._factory()
            if broker is None:
                self.state = "unconfigured"
                return None
            try:
                await asyncio.wait_for(broker.connect(), timeout=30)
            except BaseException:
                try:
                    await asyncio.wait_for(broker.close(), timeout=5)
                except Exception:
                    pass
                raise
            self._broker = broker
            self.state = "connected"
            return broker

    async def _drop_broker(self, *, expected=None) -> None:
        async with self._lock:
            if expected is not None and self._broker is not expected:
                return
            broker, self._broker = self._broker, None
        if broker is not None:
            try:
                await asyncio.wait_for(broker.close(), timeout=5)
            except Exception:
                pass

    async def _run(self, symbol: str) -> None:
        delay = 1.0
        while True:
            broker = None
            try:
                broker = await self._get_broker()
                if broker is None:
                    await asyncio.sleep(10)
                    continue
                self.stream_starts[symbol] += 1
                async for price in broker.ticks(symbol):
                    if broker is not self._broker:
                        break
                    stamp = None
                    if isinstance(price, dict):
                        from data.candles import _normalize_broker_timestamp
                        stamp = _normalize_broker_timestamp(price.get("timestamp"))
                        price = float(price["price"])
                    self._ticks[symbol] = Tick(symbol, price, self._clock.monotonic(), stamp)
                    delay = 1.0
                    for ev in tuple(self._listeners):
                        ev.set()
                raise ConnectionError("tick stream ended")
            except asyncio.CancelledError:
                raise
            except SessionExpired:
                self.state = "rejected"
                log.error("feed account session was rejected; replace %s", "AUTOTRADE_FEED_SSID")
                if broker is not None:
                    await self._drop_broker(expected=broker)
                await asyncio.sleep(60)
            except Exception as exc:
                self.state = "reconnecting"
                self._ticks.pop(symbol, None)   # a stale price must never look current
                log.warning("feed stream %s failed (%s); retrying", symbol, type(exc).__name__)
                if broker is not None:
                    await self._drop_broker(expected=broker)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, self._backoff_max)

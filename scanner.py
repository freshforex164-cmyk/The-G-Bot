"""User-selected asset stream manager and closed-candle signal publisher."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import config
from broker.pocket_option import CandleHistoryUnavailable, PocketOptionBroker
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from market_assets import CATALOG_BY_ID, MarketAsset, match_runtime_assets
from strategy.model import Signal
from strategy.price_action import PriceActionStrategy
from strategy.settings import STRATEGY_SETTINGS
from subscriber_store import SubscriberStore

logger = logging.getLogger("gbot.scanner")
ASSET_TICK_WAIT_SECONDS = 10.0
ASSET_SUBSCRIBE_ATTEMPTS = 2
ASSET_TICK_MAX_AGE_SECONDS = 5.0
ASSET_REFRESH_SECONDS = 20.0
ASSET_UNSUBSCRIBE_GRACE_SECONDS = 10 * 60


@dataclass
class PairScanState:
    asset: MarketAsset
    candles: CandleBuilder
    strategy: PriceActionStrategy
    first_tick: asyncio.Event = field(default_factory=asyncio.Event)
    latest_price: float | None = None
    latest_tick_at: datetime | None = None


class SignalScanner:
    """Stream only catalogue assets actively selected by subscriber chats."""

    def __init__(
        self,
        broker: PocketOptionBroker,
        subscribers: SubscriberStore,
        *,
        on_signal: Callable[[Signal], Any] | None = None,
        on_tick: Callable[[str, float, datetime], Any] | None = None,
        on_candle: Callable[[str, Candle], Any] | None = None,
        on_watchdog_event: Callable[[str], Any] | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self.broker = broker
        self.subscribers = subscribers
        self.on_signal = on_signal
        self.on_tick = on_tick
        self.on_candle = on_candle
        self.on_watchdog_event = on_watchdog_event
        self.output = output
        self.asset_matches: dict[str, MarketAsset] = {}
        self.pairs: dict[str, PairScanState] = {}
        self.signals: dict[str, Signal] = {}
        self.unmatched_catalog_names: list[str] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._preparation_tasks: set[asyncio.Task[None]] = set()
        self._unselected_since: dict[str, float] = {}
        self._reconcile_lock = asyncio.Lock()
        self._stopping = False
        self._paused = False
        self._ready = asyncio.Event()
        self._last_any_tick_at: float | None = None
        self._receiving_pairs: set[str] = set()
        self.last_signal_latency_ms: float | None = None

    @property
    def last_tick_age_seconds(self) -> float | None:
        if self._last_any_tick_at is None:
            return None
        return max(0.0, time.monotonic() - self._last_any_tick_at)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def active_signals(self) -> list[Signal]:
        now = datetime.now(timezone.utc)
        return [
            signal
            for signal in self.signals.values()
            if (now - signal.time).total_seconds() <= 10
        ]

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stream_health_line(self) -> str:
        receiving = len(self._receiving_pairs)
        self._receiving_pairs.clear()
        return f"STREAM HEALTH selected_assets={len(self.pairs)} receiving_tick_assets={receiving}"

    def price_for_symbol(
        self, symbol: str
    ) -> tuple[str, float, datetime] | None:
        state = self.pairs.get(symbol)
        if (
            state is None
            or state.latest_price is None
            or state.latest_tick_at is None
        ):
            return None
        age = (datetime.now(timezone.utc) - state.latest_tick_at).total_seconds()
        if age < 0 or age >= ASSET_TICK_MAX_AGE_SECONDS:
            return None
        return symbol, state.latest_price, state.latest_tick_at

    async def live_payout(self, symbol: str) -> float | None:
        """Payout for this symbol as read from the feed right now (never cached)."""
        reader = getattr(self.broker, "payout", None)
        if not callable(reader):
            return None
        try:
            return await asyncio.wait_for(reader(symbol), timeout=5.0)
        except Exception as exc:
            logger.warning("Live payout unavailable (%s).", type(exc).__name__)
            return None

    async def live_payouts(self) -> dict[str, float]:
        """All payouts as read from the feed right now; empty when unavailable."""
        try:
            payouts = await asyncio.wait_for(self.broker.payouts(), timeout=5.0)
        except Exception as exc:
            logger.warning("Live payouts unavailable (%s).", type(exc).__name__)
            return {}
        return {
            symbol: float(value)
            for symbol, value in payouts.items()
            if isinstance(value, (int, float)) and 0 < value <= 100
        }

    async def refresh_asset_matches(self) -> dict[str, MarketAsset]:
        rows = await self.broker.active_assets()
        try:
            payouts = await asyncio.wait_for(
                self.broker.payouts(), timeout=config.PAYOUTS_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning(
                "Payouts unavailable (%s); asset selection remains unfiltered.",
                type(exc).__name__,
            )
            payouts = {}
        self.asset_matches = match_runtime_assets(rows, payouts)
        self.unmatched_catalog_names = [
            asset.name
            for asset in CATALOG_BY_ID.values()
            if asset.id not in self.asset_matches
        ]
        return self.asset_matches

    def _selected_symbols(self) -> set[str]:
        selected = {
            subscriber.selected_asset
            for subscriber in self.subscribers.subscribers.values()
            if subscriber.subscribed and subscriber.selected_asset
        }
        return {str(symbol) for symbol in selected}

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._preparation_tasks.add(task)
        task.add_done_callback(self._preparation_tasks.discard)

    async def _prepare_selected_stream(self) -> None:
        try:
            await self._reconcile_selected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Selected asset stream setup failed (%s).", type(exc).__name__
            )

    def _schedule_selected_stream_setup(self) -> None:
        self._spawn(self._prepare_selected_stream(), "prepare-selected-asset-stream")

    def _new_state(self, asset: MarketAsset) -> PairScanState:
        builder = CandleBuilder(
            period_seconds=STRATEGY_SETTINGS.timeframe_seconds,
            max_candles=config.MAX_CANDLES_PER_PAIR,
        )
        return PairScanState(asset, builder, PriceActionStrategy())

    async def _seed_state(self, state: PairScanState) -> None:
        """Seed closed history after the live subscription is already open."""
        asset = state.asset
        lookback = STRATEGY_SETTINGS.timeframe_seconds * config.MAX_CANDLES_PER_PAIR
        try:
            history = await self.broker.candle_history(
                asset.symbol, STRATEGY_SETTINGS.timeframe_seconds, lookback
            )
            state.candles.seed(history)
        except asyncio.CancelledError:
            raise
        except CandleHistoryUnavailable as exc:
            logger.warning("%s", exc)
        except Exception as exc:
            logger.warning(
                "Candle history unavailable for %s (%s).",
                asset.display_name,
                type(exc).__name__,
            )
        if self.pairs.get(asset.symbol) is state:
            state.strategy.bootstrap(tuple(state.candles.closed))

    def _start_stream(self, symbol: str, state: PairScanState) -> None:
        self._tasks[symbol] = asyncio.create_task(
            self._stream_pair(symbol, state), name=f"selected-asset-{symbol}"
        )

    async def _drop_stream(self, symbol: str) -> None:
        task = self._tasks.pop(symbol, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.pairs.pop(symbol, None)
        self._unselected_since.pop(symbol, None)

    async def _make_room(self, keep: set[str]) -> bool:
        """Free one slot under MAX_SUBSCRIBED by dropping the oldest unselected stream."""
        while len(self.pairs) >= config.MAX_SUBSCRIBED:
            candidates = [
                symbol for symbol in self.pairs if symbol not in keep
            ]
            if not candidates:
                logger.warning(
                    "Subscription limit reached (%d symbols).", config.MAX_SUBSCRIBED
                )
                return False
            oldest = min(
                candidates,
                key=lambda symbol: self._unselected_since.get(symbol, float("inf")),
            )
            await self._drop_stream(oldest)
        return True

    async def _ensure_stream(self, asset: MarketAsset) -> PairScanState | None:
        """Subscribe this exact symbol now (not the scanner list) and seed afterwards."""
        state = self.pairs.get(asset.symbol)
        if state is not None:
            state.asset = asset
            return state
        if not await self._make_room(self._selected_symbols() | {asset.symbol}):
            return None
        state = self._new_state(asset)
        self.pairs[asset.symbol] = state
        self._start_stream(asset.symbol, state)
        self._spawn(self._seed_state(state), f"seed-{asset.symbol}")
        return state

    async def _restart_stream(self, symbol: str, state: PairScanState) -> None:
        task = self._tasks.pop(symbol, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.pairs.get(symbol) is state:
            self._start_stream(symbol, state)

    async def _wait_for_fresh_tick(self, symbol: str, state: PairScanState) -> bool:
        if self.price_for_symbol(symbol) is not None:
            return True
        state.first_tick.clear()
        try:
            await asyncio.wait_for(
                state.first_tick.wait(), timeout=ASSET_TICK_WAIT_SECONDS
            )
        except TimeoutError:
            return False
        return self.price_for_symbol(symbol) is not None

    async def _reconcile_selected(self) -> None:
        async with self._reconcile_lock:
            now = time.monotonic()
            desired_symbols = self._selected_symbols()
            open_assets = {
                asset.symbol: asset
                for asset in self.asset_matches.values()
                if asset.is_open
            }
            for symbol in desired_symbols:
                self._unselected_since.pop(symbol, None)

            retained_symbols = set(desired_symbols)
            for symbol in set(self.pairs) - desired_symbols:
                if symbol not in open_assets:
                    self._unselected_since.pop(symbol, None)
                    continue
                since = self._unselected_since.setdefault(symbol, now)
                if now - since < ASSET_UNSUBSCRIBE_GRACE_SECONDS:
                    retained_symbols.add(symbol)

            available = {
                symbol: open_assets[symbol]
                for symbol in retained_symbols
                if symbol in open_assets
            }
            for symbol in set(self.pairs) - set(available):
                await self._drop_stream(symbol)
            for symbol, asset in available.items():
                if symbol in self.pairs:
                    self.pairs[symbol].asset = asset
                elif symbol in desired_symbols:
                    await self._ensure_stream(asset)

    async def select_asset(self, chat_id: int, asset_id: str) -> MarketAsset | None:
        """Subscribe the chosen symbol and wait for a fresh tick (2 x 10 s)."""
        if asset_id not in CATALOG_BY_ID:
            return None
        try:
            await self.refresh_asset_matches()
        except Exception as exc:
            logger.warning(
                "Could not refresh asset metadata (%s).", type(exc).__name__
            )
            return None
        asset = self.asset_matches.get(asset_id)
        if asset is None or not asset.is_open:
            return None
        symbol = asset.symbol
        available = False
        for attempt in range(ASSET_SUBSCRIBE_ATTEMPTS):
            async with self._reconcile_lock:
                state = await self._ensure_stream(asset)
                if state is None:
                    return None
                if attempt > 0:
                    await self._restart_stream(symbol, state)
            if await self._wait_for_fresh_tick(symbol, state):
                available = True
                break
            logger.info(
                "No fresh tick for %s on subscribe attempt %d/%d.",
                asset.display_name,
                attempt + 1,
                ASSET_SUBSCRIBE_ATTEMPTS,
            )
        if not available:
            if symbol not in self._selected_symbols():
                async with self._reconcile_lock:
                    await self._drop_stream(symbol)
            return None
        await asyncio.to_thread(self.subscribers.set_asset, chat_id, symbol)
        self._unselected_since.pop(symbol, None)
        self._schedule_selected_stream_setup()
        return asset

    async def _stream_pair(self, symbol: str, state: PairScanState) -> None:
        while not self._stopping and self.pairs.get(symbol) is state:
            ticks = self.broker.subscribe_ticks(symbol)
            try:
                async for tick in ticks:
                    if self._stopping or self.pairs.get(symbol) is not state:
                        return
                    self._receiving_pairs.add(symbol)
                    self._handle_tick(symbol, state, tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Tick stream for selected asset stopped (%s); reconnecting.",
                    type(exc).__name__,
                )
            finally:
                await ticks.aclose()
            if not self._stopping and self.pairs.get(symbol) is state:
                await asyncio.sleep(0.25)

    def _handle_tick(
        self, symbol: str, state: PairScanState, tick: dict[str, Any]
    ) -> None:
        try:
            timestamp = datetime.fromtimestamp(
                _normalize_broker_timestamp(tick.get("timestamp", tick.get("time"))),
                timezone.utc,
            )
            price = float(tick.get("close", tick.get("price")))
        except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
            return
        tick_age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if tick_age < 0 or tick_age >= ASSET_TICK_MAX_AGE_SECONDS:
            return
        state.latest_price = price
        state.latest_tick_at = timestamp
        state.first_tick.set()
        self._last_any_tick_at = time.monotonic()
        if self.on_tick is not None:
            try:
                self.on_tick(symbol, price, timestamp)
            except Exception as exc:
                logger.error(
                    "Signal outcome tick update failed (%s).", type(exc).__name__
                )
        closed = state.candles.update(tick)
        if closed is not None and self.on_candle is not None:
            try:
                self.on_candle(symbol, closed)
            except Exception as exc:
                logger.error(
                    "Signal outcome candle update failed (%s).", type(exc).__name__
                )
        if closed is not None and not self._paused:
            signal = state.strategy.on_closed_candles(
                symbol,
                tuple(state.candles.closed),
                asset_name=state.asset.display_name,
                payout=None,
            )
            if signal is not None:
                self._spawn(
                    self._publish_with_live_payout(signal), f"signal-{symbol}"
                )

    async def _publish_with_live_payout(self, signal: Signal) -> None:
        signal.payout = await self.live_payout(signal.pair)
        self._publish_signal(signal)

    def _publish_signal(self, signal: Signal) -> None:
        if not self.subscribers.subscribed_chat_ids(signal.pair):
            return
        state = self.pairs.get(signal.pair)
        if state is None or self.price_for_symbol(signal.pair) is None:
            return
        self.signals[signal.id] = signal
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        self.last_signal_latency_ms = max(
            0.0,
            (datetime.now(timezone.utc) - signal_time).total_seconds() * 1000,
        )
        if self.on_signal is not None:
            try:
                result = self.on_signal(signal)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.error("Signal callback failed (%s).", type(exc).__name__)

    async def _refresh_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=ASSET_REFRESH_SECONDS
                )
            except TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                await self.refresh_asset_matches()
                await self._reconcile_selected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Selected asset refresh failed (%s).", type(exc).__name__)

    async def run(self, *, minutes: float | None = None) -> None:
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes must be greater than zero")
        self._stopping = False
        stop_event = asyncio.Event()
        monitor: asyncio.Task[None] | None = None
        refresher: asyncio.Task[None] | None = None
        deadline = (
            asyncio.get_running_loop().time() + minutes * 60
            if minutes is not None
            else float("inf")
        )
        try:
            await asyncio.wait_for(
                self.broker.connect(),
                timeout=config.INITIAL_CONNECT_TIMEOUT_SECONDS + 2,
            )
            await self.refresh_asset_matches()
            await self._reconcile_selected()
            self._ready.set()
            monitor = asyncio.create_task(
                self.broker.monitor_connection(stop_event),
                name="pocket-option-connection-monitor",
            )
            refresher = asyncio.create_task(
                self._refresh_loop(stop_event), name="selected-assets-refresh"
            )
            next_health = time.monotonic() + 60
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                if time.monotonic() >= next_health:
                    self.output(self.stream_health_line())
                    next_health = time.monotonic() + 60
        finally:
            self._stopping = True
            stop_event.set()
            for task in (
                monitor,
                refresher,
                *self._preparation_tasks,
                *self._tasks.values(),
            ):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (
                        monitor,
                        refresher,
                        *self._preparation_tasks,
                        *self._tasks.values(),
                    )
                    if task
                ),
                return_exceptions=True,
            )
            self._preparation_tasks.clear()
            self._tasks.clear()
            self.output(self.stream_health_line())
            await self.broker.close()

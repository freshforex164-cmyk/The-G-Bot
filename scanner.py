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
ASSET_TICK_WAIT_SECONDS = 5.0
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

    async def _probe_for_fresh_tick(self, symbol: str) -> bool:
        """Open a short-lived stream immediately and require a fresh first tick."""
        stream = self.broker.subscribe_ticks(symbol)
        deadline = asyncio.get_running_loop().time() + ASSET_TICK_WAIT_SECONDS
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    tick = await asyncio.wait_for(anext(stream), timeout=remaining)
                except (StopAsyncIteration, TimeoutError):
                    return False
                try:
                    timestamp = datetime.fromtimestamp(
                        _normalize_broker_timestamp(
                            tick.get("timestamp", tick.get("time"))
                        ),
                        timezone.utc,
                    )
                    float(tick.get("close", tick.get("price")))
                except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
                    continue
                age = (datetime.now(timezone.utc) - timestamp).total_seconds()
                if 0 <= age < ASSET_TICK_MAX_AGE_SECONDS:
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Asset availability probe failed (%s).", type(exc).__name__)
            return False
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

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
        task = asyncio.create_task(
            self._prepare_selected_stream(),
            name="prepare-selected-asset-stream",
        )
        self._preparation_tasks.add(task)
        task.add_done_callback(self._preparation_tasks.discard)

    async def _seed_asset(self, asset: MarketAsset) -> PairScanState:
        builder = CandleBuilder(
            period_seconds=STRATEGY_SETTINGS.timeframe_seconds,
            max_candles=config.MAX_CANDLES_PER_PAIR,
        )
        lookback = STRATEGY_SETTINGS.timeframe_seconds * config.MAX_CANDLES_PER_PAIR
        try:
            history = await self.broker.candle_history(
                asset.symbol, STRATEGY_SETTINGS.timeframe_seconds, lookback
            )
            builder.seed(history)
        except CandleHistoryUnavailable as exc:
            logger.warning("%s", exc)
        except (CandleDataError, TimeoutError) as exc:
            logger.warning(
                "Candle history unavailable for %s (%s).",
                asset.display_name,
                type(exc).__name__,
            )
        strategy = PriceActionStrategy()
        strategy.bootstrap(tuple(builder.closed))
        return PairScanState(asset, builder, strategy)

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
                task = self._tasks.pop(symbol, None)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.pairs.pop(symbol, None)
                self._unselected_since.pop(symbol, None)
            # Fetch each newly selected history sequentially on the shared client.
            for symbol, asset in available.items():
                if symbol in self.pairs:
                    self.pairs[symbol].asset = asset
                    continue
                state = await self._seed_asset(asset)
                self.pairs[symbol] = state
                self._tasks[symbol] = asyncio.create_task(
                    self._stream_pair(symbol, state),
                    name=f"selected-asset-{symbol}",
                )

    async def select_asset(self, chat_id: int, asset_id: str) -> MarketAsset | None:
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
        state = self.pairs.get(asset.symbol)
        if state is None:
            if not await self._probe_for_fresh_tick(asset.symbol):
                return None
            await asyncio.to_thread(self.subscribers.set_asset, chat_id, asset.symbol)
            self._schedule_selected_stream_setup()
            return asset
        if state.first_tick.is_set() and self.price_for_symbol(asset.symbol) is None:
            state.first_tick.clear()
        if self.price_for_symbol(asset.symbol) is None:
            try:
                await asyncio.wait_for(
                    state.first_tick.wait(), timeout=ASSET_TICK_WAIT_SECONDS
                )
            except TimeoutError:
                return None
            if self.price_for_symbol(asset.symbol) is None:
                return None
        await asyncio.to_thread(self.subscribers.set_asset, chat_id, asset.symbol)
        await self._reconcile_selected()
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
                payout=state.asset.payout,
            )
            if signal is not None:
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

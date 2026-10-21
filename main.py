"""Read-only command-line market-data viewer for GBot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from broker.pocket_option import (
    CandleHistoryUnavailable,
    PocketOptionAuthenticationError,
    PocketOptionBroker,
    PocketOptionBrokerError,
    PocketOptionConnectionError,
)
from config import (
    CANDLE_INTERVAL_SECONDS,
    ConfigurationError,
    HISTORY_LOOKBACK_SECONDS,
    MAX_CANDLES_PER_PAIR,
    MIN_PAYOUT,
    RUN_DURATION_SECONDS,
    TOP_PAIRS_COUNT,
    load_ssid,
)
from notifications import (
    NotificationQueue,
    TelegramSendError,
    build_notifier,
    format_signal_message,
)
from data.candles import (
    Candle,
    CandleBuilder,
    CandleDataError,
    _normalize_broker_timestamp,
)
from paper.store import CsvSignalStore
from paper.tracker import PaperTracker
from scanner import SignalScanner
from strategy.pullback_reversal import Signal
from strategy.replay import REPLAY_HORIZONS, ReplaySummary, replay_candles

logger = logging.getLogger("gbot")


def _format_candle(pair: str, candle: Candle) -> str:
    timestamp = datetime.fromtimestamp(candle.time, timezone.utc).isoformat(
        timespec="seconds"
    )
    return (
        f"{timestamp} {pair} O={candle.open:g} H={candle.high:g} "
        f"L={candle.low:g} C={candle.close:g}"
    )


async def _stream_pair(
    broker: PocketOptionBroker,
    pair: str,
    builder: CandleBuilder,
    deadline: float,
) -> None:
    loop = asyncio.get_running_loop()
    ticks: AsyncGenerator[dict[str, Any], None] = broker.subscribe_ticks(pair)
    try:
        async for tick in ticks:
            if loop.time() >= deadline:
                return
            closed = builder.update(tick)
            if closed is not None:
                print(_format_candle(pair, closed), flush=True)
    finally:
        await ticks.aclose()


async def run_watch() -> None:
    """Show only completed 10-second candles from currently eligible pairs."""
    broker = PocketOptionBroker(load_ssid())
    stop_monitor = asyncio.Event()
    monitor_task: asyncio.Task[None] | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        await broker.connect()
        monitor_task = asyncio.create_task(broker.monitor_connection(stop_monitor))
        payouts = await broker.payouts()
        pairs = sorted(
            (
                (pair, payout)
                for pair, payout in payouts.items()
                if payout >= MIN_PAYOUT
            ),
            key=lambda item: (-item[1], item[0]),
        )[:TOP_PAIRS_COUNT]
        if not pairs:
            raise PocketOptionBrokerError(
                f"No open pair meets the minimum payout of {MIN_PAYOUT}%."
            )

        builders: dict[str, CandleBuilder] = {}
        for pair, _payout in pairs:
            builder = CandleBuilder(
                period_seconds=CANDLE_INTERVAL_SECONDS,
                max_candles=MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await broker.candle_history(
                    pair, CANDLE_INTERVAL_SECONDS, HISTORY_LOOKBACK_SECONDS
                )
                builder.seed(history)
            except CandleHistoryUnavailable as exc:
                logger.warning("%s", exc)
            except CandleDataError as exc:
                logger.warning(
                    "Historical candle seed for %s was skipped (%s).",
                    pair,
                    type(exc).__name__,
                )
            builders[pair] = builder

        deadline = asyncio.get_running_loop().time() + RUN_DURATION_SECONDS
        tasks = [
            asyncio.create_task(_stream_pair(broker, pair, builder, deadline))
            for pair, builder in builders.items()
        ]
        if monitor_task is not None:
            tasks.append(monitor_task)
        done, _pending = await asyncio.wait(
            tasks,
            timeout=RUN_DURATION_SECONDS,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
    finally:
        stop_monitor.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if monitor_task is not None and monitor_task not in tasks:
            monitor_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if monitor_task is not None and monitor_task not in tasks:
            await asyncio.gather(monitor_task, return_exceptions=True)
        await broker.close()


def _safe_reason(error: BaseException, secret: str) -> str:
    reason = str(error).strip() or type(error).__name__
    return reason.replace(secret, "[REDACTED]")


def _format_feed_tick(pair: str, tick: dict[str, Any]) -> tuple[str, float]:
    timestamp = _normalize_broker_timestamp(tick.get("timestamp", tick.get("time")))
    price = float(tick.get("close", tick.get("price")))
    if not math.isfinite(price):
        raise CandleDataError("tick price is not finite")
    utc_time = datetime.fromtimestamp(timestamp, timezone.utc)
    local_time = utc_time.astimezone()
    return (
        f"{utc_time.isoformat(timespec='milliseconds')} UTC | "
        f"{local_time.isoformat(timespec='milliseconds')} local | "
        f"{pair} {price:.10g}",
        price,
    )


async def run_feed_check(*, duration_seconds: float = 30.0) -> bool:
    """Print a 30-second tick sample from the highest-payout eligible pair."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    ssid = load_ssid()
    broker = PocketOptionBroker(ssid)
    stop_monitor = asyncio.Event()
    monitor_task: asyncio.Task[None] | None = None
    ticks: AsyncGenerator[dict[str, Any], None] | None = None
    count = 0
    last_price: float | None = None
    failure: str | None = None
    try:
        await broker.connect()
        account_type = "demo" if broker.is_demo_account else "real"
        print(f"Account type: {account_type} (informational only)", flush=True)
        payouts = await broker.payouts()
        eligible = sorted(
            (
                (pair, payout)
                for pair, payout in payouts.items()
                if payout >= MIN_PAYOUT
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if not eligible:
            failure = f"no open pair meets minimum payout {MIN_PAYOUT}%"
        else:
            pair = eligible[0][0]
            ticks = broker.subscribe_ticks(pair)
            monitor_task = asyncio.create_task(
                broker.monitor_connection(stop_monitor),
                name="feed-check-connection-monitor",
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + duration_seconds
            while loop.time() < deadline:
                try:
                    tick = await asyncio.wait_for(
                        anext(ticks), timeout=max(0.01, deadline - loop.time())
                    )
                    line, price = _format_feed_tick(pair, tick)
                    print(line, flush=True)
                    count += 1
                    last_price = price
                except TimeoutError:
                    break
                except StopAsyncIteration:
                    failure = "tick stream ended before 30 seconds"
                    break
                except Exception as exc:
                    failure = _safe_reason(exc, ssid)
                    break
            if failure is None and loop.time() < deadline:
                failure = "tick stream ended before 30 seconds"
            if failure is None and count == 0:
                failure = "no ticks received in 30 seconds"
    except Exception as exc:
        failure = _safe_reason(exc, ssid)
    finally:
        if ticks is not None:
            await ticks.aclose()
        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        await broker.close()

    if failure is not None:
        print(f"FEED FAILED: {failure}", flush=True)
        return False
    print(
        f"FEED OK: {count} ticks in {duration_seconds:g}s, "
        f"last price {last_price:.10g}",
        flush=True,
    )
    return True


def _format_replay_summary(summary: ReplaySummary) -> str:
    directions = (
        f"SELL {summary.directions['SELL']} / BUY {summary.directions['BUY']}"
    )
    if summary.strength_buckets:
        strengths = ", ".join(
            f"{bucket}={summary.strength_buckets[bucket]}"
            for bucket in ("0-59", "60-79", "80-100", "unscored")
            if summary.strength_buckets[bucket]
        )
    else:
        strengths = "none"
    outcome_text = " ".join(
        f"{horizon * CANDLE_INTERVAL_SECONDS}s "
        f"W{summary.outcomes[horizon]['WIN']}/L{summary.outcomes[horizon]['LOSS']}"
        for horizon in REPLAY_HORIZONS
    )
    return (
        f"APPROXIMATE {summary.pair}: signals={summary.signal_count}; "
        f"directions {directions}; strengths {strengths}; {outcome_text}"
    )


def _merge_replay_summaries(
    summaries: list[ReplaySummary],
) -> ReplaySummary:
    total = ReplaySummary(pair="TOTAL", signals=[])
    for summary in summaries:
        total.signals.extend(summary.signals)
        total.directions.update(summary.directions)
        total.strength_buckets.update(summary.strength_buckets)
        for horizon in REPLAY_HORIZONS:
            total.outcomes[horizon].update(summary.outcomes[horizon])
    return total


async def run_replay() -> list[ReplaySummary]:
    """Replay seeded closed candles for every pair above the payout threshold."""
    broker = PocketOptionBroker(load_ssid())
    summaries: list[ReplaySummary] = []
    try:
        await broker.connect()
        payouts = await broker.payouts()
        pairs = sorted(
            (
                (pair, payout)
                for pair, payout in payouts.items()
                if payout >= MIN_PAYOUT
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if not pairs:
            raise PocketOptionBrokerError(
                f"No open pair meets the minimum payout of {MIN_PAYOUT}%."
            )
        for pair, _payout in pairs:
            builder = CandleBuilder(
                period_seconds=CANDLE_INTERVAL_SECONDS,
                max_candles=MAX_CANDLES_PER_PAIR,
            )
            history = await broker.candle_history(
                pair, CANDLE_INTERVAL_SECONDS, HISTORY_LOOKBACK_SECONDS
            )
            seeded = builder.seed(history)
            if seeded < MAX_CANDLES_PER_PAIR:
                logger.warning(
                    "%s history returned %d of %d requested candles.",
                    pair,
                    seeded,
                    MAX_CANDLES_PER_PAIR,
                )
            summary = replay_candles(pair, tuple(builder.closed))
            summaries.append(summary)
            print(_format_replay_summary(summary), flush=True)
        print(
            _format_replay_summary(_merge_replay_summaries(summaries)),
            flush=True,
        )
        return summaries
    finally:
        await broker.close()


def _format_signal_line(signal: Signal) -> str:
    timestamp = signal.time.astimezone(timezone.utc).isoformat(timespec="seconds")
    valid_for = max(
        0,
        int(
            (signal.valid_until - signal.time).total_seconds()
            if signal.valid_until is not None
            else 0
        ),
    )
    return (
        f"{timestamp} {signal.pair} {signal.direction} "
        f"payout={signal.payout:g}% strength={signal.strength} "
        f"expiry={signal.recommended_expiry_seconds}s valid-for={valid_for}s"
    )


async def run_scanner(*, minutes: float) -> None:
    tracker = PaperTracker(store=CsvSignalStore(config.SIGNALS_CSV_PATH))
    queue = NotificationQueue(build_notifier())

    def on_signal(signal: Signal) -> None:
        tracker.record_signal(signal)
        print(_format_signal_line(signal), flush=True)
        queue.enqueue(signal)

    scanner = SignalScanner(
        PocketOptionBroker(load_ssid()),
        on_signal=on_signal,
        on_tick=tracker.update_price,
    )
    queue.start()
    try:
        await scanner.run(minutes=minutes)
    finally:
        await queue.close()
        tracker.print_summary()


async def run_notify_test() -> bool:
    now = datetime.now(timezone.utc)
    sample = Signal(
        pair="BITB_otc",
        direction="SELL",
        price=24.0017,
        time=now,
        stochastic_k=80.0,
        stochastic_d=75.0,
        ma_slope=-0.01,
        bollinger_middle=24.0,
        payout=92.0,
        strength=84,
        strength_components={
            "stochastic": 26,
            "moving_average": 21,
            "bollinger": 20,
            "middle_band_touch": 17,
        },
        valid_until=now + timedelta(seconds=config.SIGNAL_VALID_SECONDS),
    )
    notifier = build_notifier()
    try:
        result = await notifier.send(sample)
    except TelegramSendError as exc:
        print(f"NOTIFY TEST FAILED: {exc}", flush=True)
        return False
    print(f"NOTIFY TEST OK: {result}", flush=True)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option feed and strategy scanner."
    )
    parser.add_argument(
        "--run",
        choices=("watch", "feed-check", "replay", "scanner", "notify-test"),
        default="watch",
        help="View candles, check the feed, replay history, scan signals, or test notifications.",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=3.0,
        help="Scanner run length in minutes (default: 3).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.run == "feed-check":
            return 0 if asyncio.run(run_feed_check()) else 1
        if args.run == "replay":
            asyncio.run(run_replay())
        elif args.run == "scanner":
            asyncio.run(run_scanner(minutes=args.minutes))
        elif args.run == "notify-test":
            return 0 if asyncio.run(run_notify_test()) else 1
        else:
            asyncio.run(run_watch())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        return 130
    except (ConfigurationError, PocketOptionAuthenticationError,
            PocketOptionConnectionError, PocketOptionBrokerError,
            CandleDataError) as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.error("Feed viewer stopped unexpectedly (%s).", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

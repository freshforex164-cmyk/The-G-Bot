"""Run the read-only G Bots watch and signal engine."""

from __future__ import annotations

import asyncio
import argparse
import csv
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    MAX_PAIRS,
    MODE,
    MIN_PAYOUT,
    RUN_DURATION_SECONDS,
    SIGNALS_CSV_PATH,
    SIGNAL_RUN_DEFAULT_MINUTES,
    TOP_PAIRS_COUNT,
    load_mode_ssid,
    validate_mode,
)
from data.candles import Candle, CandleBuilder, CandleDataError
from paper.tracker import PaperTracker
from strategy.pullback_reversal import PullbackReversalStrategy, Signal

logger = logging.getLogger("g_bots")

SIGNAL_CSV_FIELDS = (
    "time",
    "pair",
    "direction",
    "price",
    "stochastic_k",
    "stochastic_d",
    "ma_slope",
    "bollinger_middle",
    "payout_percent",
)


def _format_candle(pair: str, candle: Candle) -> str:
    timestamp = datetime.fromtimestamp(candle.time, timezone.utc).isoformat(
        timespec="seconds"
    )
    return (
        f"{pair} {timestamp} "
        f"O={candle.open:g} H={candle.high:g} "
        f"L={candle.low:g} C={candle.close:g}"
    )


def _display_value(value: float | None) -> str:
    return "off" if value is None else f"{value:g}"


def _format_signal(signal: Signal) -> str:
    timestamp = signal.time.astimezone(timezone.utc).isoformat(timespec="seconds")
    return (
        f"[SIGNAL] {timestamp} {signal.pair} {signal.direction} {signal.price:g} "
        f"| stoch {_display_value(signal.stochastic_k)}/"
        f"{_display_value(signal.stochastic_d)} "
        f"| MA slope {_display_value(signal.ma_slope)} "
        f"| BB mid {signal.bollinger_middle:g}"
    )


def _append_signal_csv(
    signal: Signal,
    payout_percent: float,
    path: str | Path | None = None,
) -> None:
    csv_path = Path(SIGNALS_CSV_PATH if path is None else path)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=SIGNAL_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "time": signal.time.astimezone(timezone.utc).isoformat(),
                "pair": signal.pair,
                "direction": signal.direction,
                "price": signal.price,
                "stochastic_k": signal.stochastic_k,
                "stochastic_d": signal.stochastic_d,
                "ma_slope": signal.ma_slope,
                "bollinger_middle": signal.bollinger_middle,
                "payout_percent": payout_percent,
            }
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
            closed_candle = builder.update(tick)
            if closed_candle is not None:
                print(_format_candle(pair, closed_candle), flush=True)
    finally:
        await ticks.aclose()


async def _stream_signal_pair(
    broker: PocketOptionBroker,
    pair: str,
    payout_percent: float,
    builder: CandleBuilder,
    strategy: PullbackReversalStrategy,
    tracker: PaperTracker,
    deadline: float,
) -> None:
    loop = asyncio.get_running_loop()
    ticks: AsyncGenerator[dict[str, Any], None] = broker.subscribe_ticks(pair)
    try:
        async for tick in ticks:
            if loop.time() >= deadline:
                return
            closed_candle = builder.update(tick)
            if closed_candle is not None:
                strategy.on_closed_candles(pair, tuple(builder.closed))

            forming = builder.forming_candle
            if forming is None:
                continue
            live_price = forming.close
            tracker.update_price(pair, live_price)
            signal = strategy.on_tick(
                pair,
                live_price,
                forming,
                timestamp=datetime.now(timezone.utc),
            )
            if signal is None:
                continue

            print(_format_signal(signal), flush=True)
            _append_signal_csv(signal, payout_percent)
            tracker.record_signal(
                signal.pair,
                signal.direction,
                signal.price,
                signal.time,
                payout_percent,
            )
    finally:
        await ticks.aclose()


async def run_engine(
    mode: str | None = None,
    *,
    confirm_real: bool = False,
    run_mode: str = "watch",
    minutes: int | None = None,
) -> None:
    active_mode = validate_mode(MODE if mode is None else mode)
    if run_mode not in {"watch", "signals"}:
        raise ConfigurationError("--run must be either 'watch' or 'signals'.")

    if run_mode == "signals":
        run_minutes = SIGNAL_RUN_DEFAULT_MINUTES if minutes is None else minutes
        if run_minutes <= 0:
            raise ConfigurationError("--minutes must be a positive whole number.")
        run_duration = run_minutes * 60
        pair_limit = MAX_PAIRS
        strategy: PullbackReversalStrategy | None = PullbackReversalStrategy()
    else:
        run_minutes = None
        run_duration = RUN_DURATION_SECONDS
        pair_limit = TOP_PAIRS_COUNT
        strategy = None

    ssid = load_mode_ssid(active_mode, confirm_real=confirm_real)
    broker = PocketOptionBroker(ssid, mode=active_mode)
    stop_monitor = asyncio.Event()
    monitor_task: asyncio.Task[None] | None = None
    tracker: PaperTracker | None = PaperTracker() if run_mode == "signals" else None
    try:
        await broker.connect()
        monitor_task = asyncio.create_task(
            broker.monitor_connection(stop_monitor),
            name="pocket-option-connection-monitor",
        )

        balance = await broker.balance()
        print(f"{active_mode.capitalize()} balance: {balance}", flush=True)

        payouts = await broker.payouts()
        eligible = sorted(
            (
                (pair, payout)
                for pair, payout in payouts.items()
                if payout >= MIN_PAYOUT
            ),
            key=lambda item: (-item[1], item[0]),
        )

        print(f"Pairs with payout >= {MIN_PAYOUT}%:", flush=True)
        if not eligible:
            print("No pairs currently meet the minimum payout.", flush=True)
            return
        for pair, payout in eligible:
            print(f"  {pair}: {payout:g}%", flush=True)

        selected_pairs = eligible[:pair_limit]
        if len(selected_pairs) < pair_limit:
            logger.warning(
                "Only %d pair(s) meet the payout threshold; subscribing to all of them.",
                len(selected_pairs),
            )

        builders: dict[str, CandleBuilder] = {}
        for pair, _payout in selected_pairs:
            builder = CandleBuilder(
                period_seconds=CANDLE_INTERVAL_SECONDS,
                max_candles=MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await broker.candle_history(
                    pair,
                    period_seconds=CANDLE_INTERVAL_SECONDS,
                    lookback_seconds=HISTORY_LOOKBACK_SECONDS,
                )
                seeded_count = builder.seed(history)
                logger.info("Seeded %d historical candles for %s.", seeded_count, pair)
            except CandleHistoryUnavailable as exc:
                logger.warning("%s", exc)
            except CandleDataError as exc:
                logger.warning(
                    "Historical candle seed for %s was malformed (%s); "
                    "starting from live ticks.",
                    pair,
                    type(exc).__name__,
                )
            builders[pair] = builder

        if run_mode == "signals":
            assert strategy is not None
            for pair, builder in builders.items():
                if builder.closed:
                    strategy.on_closed_candles(pair, tuple(builder.closed))

        deadline = asyncio.get_running_loop().time() + run_duration
        if run_mode == "watch":
            tasks = [
                asyncio.create_task(
                    _stream_pair(broker, pair, builder, deadline),
                    name=f"ticks-{pair}",
                )
                for pair, builder in builders.items()
            ]
            run_description = (
                f"{run_duration} seconds with "
                f"{CANDLE_INTERVAL_SECONDS}-second candles."
            )
        else:
            assert strategy is not None and tracker is not None
            payouts_by_pair = dict(selected_pairs)
            tasks = [
                asyncio.create_task(
                    _stream_signal_pair(
                        broker,
                        pair,
                        payouts_by_pair[pair],
                        builder,
                        strategy,
                        tracker,
                        deadline,
                    ),
                    name=f"signals-{pair}",
                )
                for pair, builder in builders.items()
            ]
            run_description = f"{run_minutes} minutes in SIGNAL-ONLY mode."
        tasks.append(monitor_task)

        print(
            f"Streaming {len(builders)} pair(s) in {active_mode} mode for "
            f"{run_description}",
            flush=True,
        )
        try:
            done, _pending = await asyncio.wait(
                tasks,
                timeout=run_duration,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        raise error
        finally:
            stop_monitor.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if run_mode == "watch":
            print("Two-minute data run complete.", flush=True)
        else:
            print("Signal-only run complete.", flush=True)
    finally:
        stop_monitor.set()
        if monitor_task is not None:
            if not monitor_task.done():
                monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        try:
            await broker.close()
        finally:
            if tracker is not None:
                tracker.print_summary()


def _positive_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("minutes must be a whole number") from None
    if minutes <= 0:
        raise argparse.ArgumentTypeError("minutes must be greater than zero")
    return minutes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option market data and signal runner."
    )
    parser.add_argument(
        "--run",
        choices=("watch", "signals"),
        default="watch",
        help="Watch closed candles or generate signal-only paper records.",
    )
    parser.add_argument(
        "--minutes",
        type=_positive_minutes,
        default=SIGNAL_RUN_DEFAULT_MINUTES,
        help="Signal run duration in whole minutes.",
    )
    parser.add_argument(
        "--confirm-real",
        action="store_true",
        help="Required when MODE=real; real mode remains read-only.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(
            run_engine(
                MODE,
                confirm_real=args.confirm_real,
                run_mode=args.run,
                minutes=args.minutes,
            )
        )
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        return 130
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 1
    except PocketOptionAuthenticationError as exc:
        logger.error("%s", exc)
        return 1
    except PocketOptionConnectionError as exc:
        logger.error("%s", exc)
        return 1
    except PocketOptionBrokerError as exc:
        logger.error("%s", exc)
        return 1
    except CandleDataError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        # Do not log exception messages because they may contain broker details.
        logger.error("Engine stopped unexpectedly (%s).", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

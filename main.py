"""Run the read-only Phase 1 G Bots market-data engine."""

from __future__ import annotations

import asyncio
import argparse
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime, timezone
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
    MODE,
    MIN_PAYOUT,
    RUN_DURATION_SECONDS,
    TOP_PAIRS_COUNT,
    load_mode_ssid,
    validate_mode,
)
from data.candles import Candle, CandleBuilder, CandleDataError

logger = logging.getLogger("g_bots")


def _format_candle(pair: str, candle: Candle) -> str:
    timestamp = datetime.fromtimestamp(candle.time, timezone.utc).isoformat(
        timespec="seconds"
    )
    return (
        f"{pair} {timestamp} "
        f"O={candle.open:g} H={candle.high:g} "
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
            closed_candle = builder.update(tick)
            if closed_candle is not None:
                print(_format_candle(pair, closed_candle), flush=True)
    finally:
        await ticks.aclose()


async def run_engine(
    mode: str | None = None, *, confirm_real: bool = False
) -> None:
    active_mode = validate_mode(MODE if mode is None else mode)
    ssid = load_mode_ssid(active_mode, confirm_real=confirm_real)
    broker = PocketOptionBroker(ssid, mode=active_mode)
    stop_monitor = asyncio.Event()
    monitor_task: asyncio.Task[None] | None = None
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

        selected_pairs = eligible[:TOP_PAIRS_COUNT]
        if len(selected_pairs) < TOP_PAIRS_COUNT:
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

        deadline = asyncio.get_running_loop().time() + RUN_DURATION_SECONDS
        tasks = [
            asyncio.create_task(
                _stream_pair(broker, pair, builder, deadline),
                name=f"ticks-{pair}",
            )
            for pair, builder in builders.items()
        ]
        tasks.append(monitor_task)

        print(
            f"Streaming {len(builders)} pair(s) in {active_mode} mode for "
            f"{RUN_DURATION_SECONDS} seconds "
            f"with {CANDLE_INTERVAL_SECONDS}-second candles.",
            flush=True,
        )
        try:
            done, _pending = await asyncio.wait(
                tasks,
                timeout=RUN_DURATION_SECONDS,
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

        print("Two-minute data run complete.", flush=True)
    finally:
        stop_monitor.set()
        if monitor_task is not None:
            if not monitor_task.done():
                monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        await broker.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option balance and market-data runner."
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
        asyncio.run(run_engine(MODE, confirm_real=args.confirm_real))
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

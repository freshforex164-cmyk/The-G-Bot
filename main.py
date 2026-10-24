"""Read-only command-line market-data viewer for GBot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from admin_http import AdminHttpServer
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
from data.candle_store import CsvCandleStore
from market_assets import eligible_assets, normalize_assets
from paper.store import CsvSignalStore
from paper.tracker import PaperTracker
from scanner import SignalScanner
from strategy.close_engine import CloseOnlyStrategy, explain_close_state
from strategy.rules import DirectionEvaluation, evaluate_direction, trigger_a, trigger_b
from strategy.pullback_reversal import Signal
from strategy.replay import (
    REPLAY_HORIZONS,
    ReplaySummary,
    run_strategy_lab,
    strength_percentiles,
    strategy_variants,
    win_rate,
)
from notifications import TelegramNotifier, telegram_get_updates
from strategy.strength import score_strength
from subscriber_store import SubscriberStore
from telegram_bot import (
    SubscriberFanoutNotifier,
    TelegramBotClient,
    TelegramCommandHandler,
    TelegramPollingService,
)

logger = logging.getLogger("gbot")


@dataclass(frozen=True)
class FeedCheckResult:
    """Machine-readable outcome from a bounded market-feed check."""

    ok: bool
    tick_count: int
    last_price: float | None
    reason: str | None


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


async def run_feed_check(*, duration_seconds: float = 30.0) -> FeedCheckResult:
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
    started_at = asyncio.get_running_loop().time()
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
                    failure = (
                        f"tick stream ended before {duration_seconds:g} seconds"
                    )
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
    elapsed = max(0.0, asyncio.get_running_loop().time() - started_at)
    if failure is not None:
        print(f"FEED FAILED: {failure}", flush=True)
        return FeedCheckResult(False, count, last_price, failure)
    if count == 0:
        failure = f"no ticks received in {elapsed:.2f}s"
        print(f"FEED FAILED: {failure}", flush=True)
        return FeedCheckResult(False, count, last_price, failure)
    print(
        f"FEED OK: {count} ticks in {duration_seconds:g}s, "
        f"last price {last_price:g}",
        flush=True,
    )
    return FeedCheckResult(True, count, last_price, None)


async def run_pairs() -> bool:
    """Print OTC asset metadata for comparison with the Pocket Option app."""
    broker = PocketOptionBroker(load_ssid())
    try:
        await broker.connect()
        rows = await broker.active_assets()
        payouts = await broker.payouts()
        otc_rows = sorted(
            (
                row for row in rows
                if isinstance(row.get("symbol"), str)
                and row["symbol"].endswith("_otc")
            ),
            key=lambda row: str(row.get("symbol")),
        )
        if not otc_rows:
            print("No OTC pairs were returned.")
            return False
        for row in otc_rows:
            symbol = str(row["symbol"])
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                logger.warning(
                    "Skipping OTC pair %s: Pocket Option provided no display name.",
                    symbol,
                )
                name = "(missing display name; skipped by scanner)"
            payout = payouts.get(symbol, row.get("payout"))
            print(
                f"{symbol} | {name} | payout={payout if payout is not None else 'unknown'} "
                f"| open={bool(row.get('is_active', False))}"
            )
        return True
    finally:
        await broker.close()

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


def _format_lab_rate(summary: ReplaySummary, horizon: int) -> str:
    rate = win_rate(summary, horizon)
    return "n/a" if rate is None else f"{rate:.1f}%"


def _print_strategy_lab(
    results: dict[str, ReplaySummary],
) -> None:
    print("APPROXIMATE STRATEGY LAB")
    print(
        "Variant | Signals | 30s WR | 60s WR | 120s WR | "
        f"Break-even @ {config.BREAKEVEN_PAYOUT_PERCENT}% payout"
    )
    break_even = 100.0 / (1.0 + config.BREAKEVEN_PAYOUT_PERCENT / 100.0)
    for variant in strategy_variants():
        summary = results[variant.name]
        print(
            f"{variant.name} | {summary.signal_count} | "
            f"{_format_lab_rate(summary, 3)} | "
            f"{_format_lab_rate(summary, 6)} | "
            f"{_format_lab_rate(summary, 12)} | {break_even:.1f}%"
        )
    default_variant = next(
        variant
        for variant in strategy_variants()
        if variant.trigger == config.TRIGGER_VARIANT
        and variant.s2_enabled == config.S2_ENABLED
        and variant.s4_enabled == config.S4_ENABLED
        and variant.stochastic_lookback == config.STOCHASTIC_LOOKBACK_CANDLES
    )
    percentiles = strength_percentiles([results[default_variant.name]])
    if percentiles is None:
        print("Strength calibration: no replay signals to measure.")
    else:
        print(
            "Default-variant strength percentiles: "
            + " ".join(f"p{p}={value:.1f}" for p, value in percentiles.items())
        )


async def run_replay() -> dict[str, ReplaySummary]:
    """Load recorded candles or seed up to 3,000 live candles, then compare variants."""
    store = CsvCandleStore(config.DATA_CANDLES_CSV_PATH)
    candles_by_pair = store.load()
    if candles_by_pair:
        candles_by_pair = {
            pair: series[-config.REPLAY_MAX_CANDLES_PER_PAIR :]
            for pair, series in candles_by_pair.items()
        }
        print(f"Replay source: {config.DATA_CANDLES_CSV_PATH}")
        for pair, series in sorted(candles_by_pair.items()):
            print(
                f"Loaded {pair}: {len(series)} closed 10-second candles "
                f"(maximum {config.REPLAY_MAX_CANDLES_PER_PAIR})"
            )
    else:
        broker = PocketOptionBroker(load_ssid())
        try:
            await broker.connect()
            payouts = await broker.payouts()
            pairs = sorted(
                (
                    (pair, float(payout))
                    for pair, payout in payouts.items()
                    if float(payout) >= MIN_PAYOUT
                ),
                key=lambda item: (-item[1], item[0]),
            )[: config.MAX_SCAN_PAIRS]
            if not pairs:
                raise PocketOptionBrokerError(
                    f"No pair meets the minimum payout of {MIN_PAYOUT}%."
                )
            for pair, _payout in pairs:
                builder = CandleBuilder(
                    period_seconds=CANDLE_INTERVAL_SECONDS,
                    max_candles=config.REPLAY_MAX_CANDLES_PER_PAIR,
                )
                try:
                    history = await broker.candle_history(
                        pair,
                        CANDLE_INTERVAL_SECONDS,
                        config.REPLAY_HISTORY_LOOKBACK_SECONDS,
                    )
                    actual = builder.seed(history)
                except CandleHistoryUnavailable as exc:
                    logger.warning("%s", exc)
                    actual = 0
                except CandleDataError as exc:
                    logger.warning(
                        "History for %s was skipped (%s).", pair, type(exc).__name__
                    )
                    actual = 0
                print(
                    f"Seeded {pair}: {actual} closed 10-second candles "
                    f"(requested up to {config.REPLAY_MAX_CANDLES_PER_PAIR})"
                )
                candles_by_pair[pair] = list(builder.closed)
        finally:
            await broker.close()

    results = run_strategy_lab(candles_by_pair)
    _print_strategy_lab(results)
    return results


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


async def run_scanner(*, minutes: float, record: bool = False) -> None:
    tracker = PaperTracker(store=CsvSignalStore(config.SIGNALS_CSV_PATH))
    candle_store = CsvCandleStore(config.DATA_CANDLES_CSV_PATH) if record else None

    def on_signal(signal: Signal) -> None:
        tracker.record_signal(signal)
        print(_format_signal_line(signal), flush=True)
        queue.enqueue(signal)

    def on_candle(pair: str, candle: Candle) -> None:
        if candle_store is not None:
            candle_store.append(pair, candle)

    scanner = SignalScanner(
        PocketOptionBroker(load_ssid()),
        on_signal=on_signal,
        on_tick=tracker.update_price,
        on_candle=on_candle if record else None,
    )

    async def validate_at_send(signal: Signal) -> bool:
        payouts = await scanner.broker.payouts()
        rows = await scanner.broker.active_assets()
        assets = normalize_assets(rows, payouts)
        asset = assets.get(signal.pair)
        if asset is None or not asset.is_open or asset.payout < config.MIN_PAYOUT:
            return False
        signal.payout = asset.payout
        signal.asset_name = asset.display_name
        return True

    queue = NotificationQueue(build_notifier(), pre_send=validate_at_send)
    queue.start()
    try:
        await scanner.run(minutes=minutes)
    finally:
        await queue.close()
        tracker.print_summary()


async def run_serve() -> None:
    """Run the market scanner and Telegram command poller until stopped."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError(
            "Missing required secret TELEGRAM_BOT_TOKEN for --run serve."
        )
    admin_value = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
    try:
        admin_id = int(admin_value) if admin_value else None
    except ValueError:
        raise ConfigurationError("TELEGRAM_ADMIN_ID must be a numeric Telegram user ID.") from None
    if admin_id is None:
        raise ConfigurationError(
            "Missing required secret TELEGRAM_ADMIN_ID for admin commands."
        )
    admin_chat_value = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    try:
        admin_chat_id = int(admin_chat_value) if admin_chat_value else admin_id
    except ValueError:
        raise ConfigurationError(
            "TELEGRAM_CHAT_ID must be a numeric Telegram chat ID for admin commands."
        ) from None
    try:
        port = int(os.environ.get("PORT", "5000"))
    except ValueError:
        raise ConfigurationError("PORT must be a valid TCP port number.") from None
    if not 1 <= port <= 65535:
        raise ConfigurationError("PORT must be between 1 and 65535.")
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip() or None
    if admin_token is None:
        logger.warning(
            "ADMIN_TOKEN is missing; GET /admin/status will remain unavailable."
        )

    bot = TelegramBotClient(token)
    try:
        await bot.register_commands(admin_chat_id)
    except TelegramSendError as exc:
        logger.warning(
            "Telegram command registration failed (%s); polling will still start.",
            exc.status_code or type(exc).__name__,
        )

    subscribers = await asyncio.to_thread(SubscriberStore)
    tracker = PaperTracker(store=CsvSignalStore(config.SIGNALS_CSV_PATH))
    fanout = SubscriberFanoutNotifier(bot, subscribers)
    queue = NotificationQueue(
        fanout,
        max_per_minute=25 * 60,
        valid_seconds=config.SIGNAL_VALID_SECONDS,
    )
    service_state = {"scanner_running": False}

    def on_signal(signal: Signal) -> None:
        tracker.record_signal(signal)
        print(_format_signal_line(signal), flush=True)
        queue.enqueue(signal)

    async def admin_alert(message: str) -> None:
        try:
            await bot.send_message(admin_chat_id, message, parse_mode=None)
        except TelegramSendError as exc:
            logger.warning(
                "Could not send a watchdog alert (%s).",
                exc.status_code or type(exc).__name__,
            )

    scanner = SignalScanner(
        PocketOptionBroker(load_ssid()),
        on_signal=on_signal,
        on_tick=tracker.update_price,
        on_watchdog_event=admin_alert,
    )
    handler = TelegramCommandHandler(
        bot,
        subscribers,
        admin_id=admin_id,
        referral_link=os.environ.get("REFERRAL_LINK"),
        scanner=scanner,
        tracker=tracker,
        fanout=fanout,
        scanner_running=lambda: service_state["scanner_running"],
        admin_chat_id=admin_chat_id,
    )
    polling = TelegramPollingService(bot, handler)
    stop_event = asyncio.Event()

    def broker_connected() -> bool:
        try:
            return bool(scanner.broker.is_connected)
        except Exception:
            return False

    def public_health() -> dict[str, Any]:
        if scanner.is_paused:
            scanner_status = "paused"
        elif service_state["scanner_running"]:
            scanner_status = "running"
        else:
            scanner_status = "starting"
        return {
            "ok": True,
            "scanner": scanner_status,
            "feed_connected": broker_connected(),
        }

    def admin_status() -> dict[str, Any]:
        return {
            **public_health(),
            "active_pairs": len(scanner.pairs),
            "active_signals": len(scanner.active_signals),
            "subscriber_count": subscribers.count(),
            "last_tick_age_seconds": scanner.last_tick_age_seconds,
            "ssid_configured": bool(os.environ.get("PO_SSID", "").strip()),
        }

    status_server = AdminHttpServer(
        health_provider=public_health,
        admin_provider=admin_status,
        admin_token=admin_token,
        port=port,
    )
    listener = await status_server.start()
    listener_port = listener.sockets[0].getsockname()[1]
    logger.info("GBot health server listening on port %d.", listener_port)

    async def scan_forever() -> None:
        service_state["scanner_running"] = True
        try:
            await scanner.run(minutes=None)
        finally:
            service_state["scanner_running"] = False

    queue.start()
    scan_task = asyncio.create_task(scan_forever(), name="gbot-market-scanner")
    polling_task = asyncio.create_task(
        polling.run(stop_event), name="gbot-telegram-polling"
    )
    http_task = asyncio.create_task(
        status_server.serve_forever(), name="gbot-health-http"
    )
    service_tasks = (scan_task, polling_task, http_task)
    try:
        done, _pending = await asyncio.wait(
            set(service_tasks), return_when=asyncio.FIRST_EXCEPTION
        )
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise error
    finally:
        stop_event.set()
        for task in service_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*service_tasks, return_exceptions=True)
        await status_server.close()
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
        recommended_expiry_seconds=config.EXPIRATION_SECONDS,
        asset_name="Bitcoin OTC",
        valid_until=now + timedelta(seconds=config.SIGNAL_VALID_SECONDS),
    )
    notifier = build_notifier()
    if not isinstance(notifier, TelegramNotifier):
        print("NOTIFY TEST FAILED: Telegram is disabled because its secrets are missing.")
        return False
    try:
        result = await notifier.send(sample)
    except TelegramSendError as exc:
        print(f"NOTIFY TEST FAILED: {exc}", flush=True)
        return False
    print(f"NOTIFY TEST OK: {result}", flush=True)
    return True


def _chat_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "channel_post", "edited_channel_post", "my_chat_member"):
        item = update.get(key)
        if isinstance(item, dict) and isinstance(item.get("chat"), dict):
            return item["chat"]
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            return message["chat"]
    return None


async def run_telegram_setup() -> bool:
    """List Telegram chats from bot updates without exposing the bot token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM SETUP FAILED: missing TELEGRAM_BOT_TOKEN.")
        print("Post in the channel or press Start on the bot first, then retry.")
        return False
    try:
        updates = await telegram_get_updates(token)
    except TelegramSendError as exc:
        print(f"TELEGRAM SETUP FAILED: {exc}")
        print("Post in the channel or press Start on the bot first, then retry.")
        return False

    chats: dict[str, dict[str, Any]] = {}
    for update in updates:
        chat = _chat_from_update(dict(update))
        if chat is None or "id" not in chat:
            continue
        chats[str(chat["id"])] = chat
    if not chats:
        print("No chats found in the bot's pending updates.")
    else:
        for chat_id, chat in sorted(chats.items()):
            title_or_username = chat.get("title") or chat.get("username") or "(not provided)"
            print(
                f"Chat id={chat_id} type={chat.get('type', 'unknown')} "
                f"title/username={title_or_username}"
            )
    configured_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if configured_chat_id and configured_chat_id in chats:
        print("Configured TELEGRAM_CHAT_ID matches a chat returned by getUpdates.")
    elif configured_chat_id:
        print(
            "Configured TELEGRAM_CHAT_ID was not found in these updates. "
            "It may be the wrong chat ID, or the bot may not be in the chat."
        )
    else:
        print("TELEGRAM_CHAT_ID is not configured.")
    print("Reminder: post in the channel or press Start on the bot first.")
    return True


def _explain_for_pair(
    pair: str,
    builder: CandleBuilder,
    latest_price: float | None,
) -> list[str]:
    closed = tuple(builder.closed)
    lines = [f"EXPLAIN {pair}"]
    for direction in ("SELL", "BUY"):
        if config.TRIGGER_VARIANT == "B":
            evaluation = evaluate_direction(closed[:-1], direction) if len(closed) > 1 else evaluate_direction((), direction)
            rejection = closed[-1] if closed else None
            triggered, trigger_detail = trigger_b(evaluation, rejection)
        else:
            evaluation = evaluate_direction(closed, direction)
            triggered, trigger_detail = trigger_a(
                evaluation, latest_price, builder.forming_candle
            )
        lines.append(f"{direction}:")
        lines.extend(f"  {condition.line()}" for condition in evaluation.conditions)
        trigger_mark = "✅" if triggered else "❌"
        lines.append(
            f"  TRIGGER {config.TRIGGER_VARIANT} {trigger_detail} {trigger_mark}"
        )
        if triggered:
            result = "SIGNAL"
        elif evaluation.first_failure is not None:
            result = f"FAIL {evaluation.first_failure.key}"
        else:
            result = f"FAIL TRIGGER {config.TRIGGER_VARIANT}"
        lines.append(f"  RESULT {result}")
    return lines


async def run_explain(*, pair: str | None = None, seconds: int = 30) -> bool:
    """Print close-only setup state on a 15-second cadence."""
    if seconds <= 0:
        raise ValueError("seconds must be greater than zero")
    broker = PocketOptionBroker(load_ssid())
    builders: dict[str, CandleBuilder] = {}
    strategies: dict[str, CloseOnlyStrategy] = {}
    assets_by_symbol: dict[str, Any] = {}
    streams: dict[str, AsyncGenerator[dict[str, Any], None]] = {}
    tasks: list[asyncio.Task[None]] = []
    try:
        await broker.connect()
        live_payouts = await broker.payouts()
        metadata = normalize_assets(await broker.active_assets(), live_payouts)
        selected_assets = (
            [metadata[pair]] if pair in metadata else []
        ) if pair else eligible_assets(
            metadata, MIN_PAYOUT, TOP_PAIRS_COUNT
        )
        selected = [(asset.symbol, asset.payout) for asset in selected_assets]
        if not selected:
            print("EXPLAIN FAILED: no eligible pairs were returned.")
            return False

        for symbol, payout in selected:
            builder = CandleBuilder(
                period_seconds=CANDLE_INTERVAL_SECONDS,
                max_candles=MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await broker.candle_history(
                    symbol,
                    CANDLE_INTERVAL_SECONDS,
                    HISTORY_LOOKBACK_SECONDS,
                )
                count = builder.seed(history)
            except (CandleHistoryUnavailable, CandleDataError) as exc:
                logger.warning("History for %s unavailable (%s).", symbol, type(exc).__name__)
                count = 0
            print(
                f"EXPLAIN seeded {metadata[symbol].display_name}: "
                f"{count} closed candles"
            )
            builders[symbol] = builder
            strategies[symbol] = CloseOnlyStrategy()
            assets_by_symbol[symbol] = metadata[symbol]

        for symbol, _payout in selected:
            streams[symbol] = broker.subscribe_ticks(symbol)

        async def pump(symbol: str) -> None:
            stream = streams[symbol]
            try:
                async for tick in stream:
                    closed = builders[symbol].update(tick)
                    if closed is not None:
                        strategies[symbol].on_closed_candles(
                            symbol,
                            tuple(builders[symbol].closed),
                            payout=assets_by_symbol[symbol].payout,
                        )
                        strategies[symbol].take_closed_signal(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Explain stream for %s stopped (%s).",
                    symbol,
                    type(exc).__name__,
                )
            finally:
                await stream.aclose()

        tasks = [
            asyncio.create_task(pump(symbol), name=f"explain-{symbol}")
            for symbol, _ in selected
        ]
        started = asyncio.get_running_loop().time()
        next_print = started
        deadline = started + seconds
        while next_print <= deadline:
            await asyncio.sleep(max(0.0, next_print - asyncio.get_running_loop().time()))
            elapsed = int(next_print - started)
            print(f"EXPLAIN sample +{elapsed}s")
            for symbol, _ in selected:
                for line in explain_close_state(
                    symbol,
                    tuple(builders[symbol].closed),
                    strategies[symbol],
                ):
                    print(line)
            next_print += config.CANDLE_SECONDS
            if next_print > deadline:
                break
        return True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in streams.values():
            try:
                await stream.aclose()
            except Exception:
                pass
        await broker.close()


def _test_signal_score(evaluation: DirectionEvaluation) -> int:
    score, _components = score_strength(
        stochastic_extremity=evaluation.stochastic_extremity,
        stochastic_turn=evaluation.stochastic_turn,
        moving_average_turn=evaluation.ma_turn_percent,
        bollinger_slope=evaluation.middle_slope_percent,
        bollinger_curvature=evaluation.band_curvature_percent,
        touch_cleanliness=0.5,
    )
    return score


def _test_signal_evaluations(
    pair: str,
    payout: float,
    builder: CandleBuilder,
    tick: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        price = float(tick.get("close", tick.get("price")))
        timestamp = datetime.fromtimestamp(
            _normalize_broker_timestamp(tick.get("timestamp", tick.get("time"))),
            timezone.utc,
        )
    except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
        return []
    evaluations: list[dict[str, Any]] = []
    closed = tuple(builder.closed)
    for direction in ("SELL", "BUY"):
        if config.TRIGGER_VARIANT == "B":
            if len(closed) < 2:
                continue
            evaluation = evaluate_direction(closed[:-1], direction)
            triggered, trigger_detail = trigger_b(evaluation, closed[-1])
        else:
            evaluation = evaluate_direction(closed, direction)
            triggered, trigger_detail = trigger_a(
                evaluation, price, builder.forming_candle
            )
        matched = sum(condition.eligible for condition in evaluation.conditions)
        matched += int(triggered)
        evaluations.append(
            {
                "pair": pair,
                "payout": payout,
                "direction": direction,
                "evaluation": evaluation,
                "triggered": triggered,
                "trigger_detail": trigger_detail,
                "match": matched,
                "strength": _test_signal_score(evaluation),
                "price": price,
                "timestamp": timestamp,
                "conditions": evaluation.conditions,
            }
        )
    return evaluations


def _format_test_signal(candidate: dict[str, Any]) -> str:
    timestamp: datetime = candidate["timestamp"]
    expiry = config.RECOMMENDED_EXPIRY_SECONDS
    expiry_label = f"{expiry // 60}m" if expiry % 60 == 0 else f"{expiry}s"
    condition_parts = [
        f"{condition.key} {'✅' if condition.eligible else '❌'}"
        for condition in candidate["conditions"]
    ]
    condition_parts.append(
        f"TRIGGER {'✅' if candidate['triggered'] else '❌'}"
    )
    line_one = (
        f"🧪 TEST {candidate['direction']} {candidate['asset_name']} "
        f"@{candidate['price']:.4f} | Payout {candidate['payout']:g}% | "
        f"Match {candidate['match']}/5 | "
        f"Expiry {expiry_label} | "
        f"{timestamp.strftime('%H:%M:%S')} UTC"
    )
    return (
        f"{line_one}\n{' | '.join(condition_parts)}\n"
        "Not a confirmed signal"
    )


async def run_test_signals() -> int:
    """Send up to three explicitly marked diagnostic messages, never orders."""
    notifier = build_notifier()
    if not isinstance(notifier, TelegramNotifier):
        print("TEST SIGNALS FAILED: Telegram is disabled because its secrets are missing.")
        return 1
    broker = PocketOptionBroker(load_ssid())
    streams: dict[str, AsyncGenerator[dict[str, Any], None]] = {}
    builders: dict[str, CandleBuilder] = {}
    payouts: dict[str, float] = {}
    initial_ticks: dict[str, dict[str, Any]] = {}
    try:
        await broker.connect()
        live_payouts = await broker.payouts()
        assets = normalize_assets(await broker.active_assets(), live_payouts)
        eligible = eligible_assets(
            assets, MIN_PAYOUT, config.MAX_SCAN_PAIRS
        )
        selected = sorted(
            (
                (asset.symbol, asset.payout)
                for asset in eligible
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if not selected:
            print("TEST SIGNALS FAILED: no open, named OTC pair meets the minimum payout.")
            return 1

        async def prepare_history(symbol: str, payout: float) -> None:
            builder = CandleBuilder(
                period_seconds=CANDLE_INTERVAL_SECONDS,
                max_candles=MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await broker.candle_history(
                    symbol,
                    CANDLE_INTERVAL_SECONDS,
                    HISTORY_LOOKBACK_SECONDS,
                )
                builder.seed(history)
            except (CandleHistoryUnavailable, CandleDataError) as exc:
                logger.warning("History for %s unavailable (%s).", symbol, type(exc).__name__)
            builders[symbol] = builder
            payouts[symbol] = payout

        # The library shares one websocket client; seed histories one at a time
        # before opening the simultaneous live tick subscriptions.
        for symbol, payout in selected:
            await prepare_history(symbol, payout)
            streams[symbol] = broker.subscribe_ticks(symbol)

        async def read_initial_tick(symbol: str) -> None:
            stream = streams[symbol]
            try:
                tick = await asyncio.wait_for(anext(stream), timeout=15.0)
                builders[symbol].update(tick)
                initial_ticks[symbol] = tick
            except (TimeoutError, StopAsyncIteration):
                logger.warning("No live price arrived for %s.", symbol)

        await asyncio.gather(*(read_initial_tick(symbol) for symbol, _ in selected))
        candidates = [
            item
            for symbol, _ in selected
            if symbol in initial_ticks
            for item in _test_signal_evaluations(
                symbol,
                payouts[symbol],
                builders[symbol],
                initial_ticks[symbol],
            )
        ]
        for candidate in candidates:
            candidate["asset_name"] = assets[candidate["pair"]].display_name
        best_by_pair: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            current = best_by_pair.get(candidate["pair"])
            if current is None or (
                candidate["match"], candidate["strength"]
            ) > (current["match"], current["strength"]):
                best_by_pair[candidate["pair"]] = candidate
        best = sorted(
            best_by_pair.values(),
            key=lambda item: (-item["match"], -item["strength"], -item["payout"]),
        )[:3]
        if not best:
            print("TEST SIGNALS FAILED: no pair produced a live price and candle history.")
            return 1

        sent = 0
        for candidate in best:
            symbol = candidate["pair"]
            stream = streams[symbol]
            try:
                tick = await asyncio.wait_for(anext(stream), timeout=10.0)
                builders[symbol].update(tick)
                latest_payouts = await broker.payouts()
                latest_assets = normalize_assets(
                    await broker.active_assets(), latest_payouts
                )
                asset = latest_assets.get(symbol)
                if (
                    asset is None
                    or not asset.is_open
                    or asset.payout < MIN_PAYOUT
                ):
                    logger.info(
                        "Test signal for %s was skipped after send-time asset validation.",
                        symbol,
                    )
                    continue
                refreshed = _test_signal_evaluations(
                    symbol, asset.payout, builders[symbol], tick
                )
                match = next(
                    (item for item in refreshed if item["direction"] == candidate["direction"]),
                    None,
                )
                if match is not None:
                    candidate = match
                candidate["asset_name"] = asset.display_name
            except (TimeoutError, StopAsyncIteration):
                logger.warning("No fresh send-time price arrived for %s.", symbol)
                continue
            text = _format_test_signal(candidate)
            print(text, flush=True)
            try:
                await notifier.send_text(text)
                sent += 1
            except TelegramSendError as exc:
                print(f"TEST SIGNAL SEND FAILED for {symbol}: {exc}", flush=True)
        print(f"TEST SIGNALS SENT: {sent}/{len(best)}")
        return 0 if sent == len(best) else 1
    finally:
        for stream in streams.values():
            try:
                await stream.aclose()
            except Exception:
                pass
        await broker.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option feed and strategy scanner."
    )
    parser.add_argument(
        "--run",
        choices=(
            "watch",
            "feed-check",
            "pairs",
            "replay",
            "strategy-lab",
            "scanner",
            "notify-test",
            "telegram-setup",
            "explain",
            "test-signals",
            "serve",
        ),
        default="watch",
        help="Select a read-only feed, strategy, or notification command.",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=3.0,
        help="Scanner run length in minutes (default: 3).",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Append each closed scanner candle to data/candles.csv.",
    )
    parser.add_argument(
        "--pair",
        help="Pair to inspect with --run explain; defaults to the top three by payout.",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="Explain sampling duration in seconds (default: 30).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.run == "feed-check":
            return 0 if asyncio.run(run_feed_check()).ok else 1
        if args.run == "serve":
            asyncio.run(run_serve())
            return 0
        if args.run == "pairs":
            return 0 if asyncio.run(run_pairs()) else 1
        if args.run in {"replay", "strategy-lab"}:
            asyncio.run(run_replay())
        elif args.run == "scanner":
            asyncio.run(run_scanner(minutes=args.minutes, record=args.record))
        elif args.run == "notify-test":
            return 0 if asyncio.run(run_notify_test()) else 1
        elif args.run == "telegram-setup":
            return 0 if asyncio.run(run_telegram_setup()) else 1
        elif args.run == "explain":
            return (
                0
                if asyncio.run(run_explain(pair=args.pair, seconds=args.seconds))
                else 1
            )
        elif args.run == "test-signals":
            return asyncio.run(run_test_signals())
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

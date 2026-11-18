"""Read-only Pocket Option scanner, Telegram bot, and replay commands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Sequence

import config
from admin_http import AdminHttpServer
from broker.pocket_option import (
    PocketOptionAuthenticationError,
    PocketOptionBroker,
    PocketOptionBrokerError,
    PocketOptionConnectionError,
)
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from data.signals import SignalOutcomeTracker
from market_assets import CATALOG, CATALOG_BY_ID, match_runtime_assets
from notifications import (
    NotificationQueue,
    TelegramNotifier,
    TelegramSendError,
    build_notifier,
)
from scanner import SignalScanner
from strategy.model import Signal
from strategy.settings import STRATEGY_SETTINGS
from subscriber_store import SubscriberStore
from telegram_bot import (
    SubscriberFanoutNotifier,
    TelegramBotClient,
    TelegramCommandHandler,
    TelegramPollingService,
)

logger = logging.getLogger("g_bots.main")


def _numeric_chat_id(*names: str) -> int:
    value = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    if not value:
        raise config.ConfigurationError(
            f"Missing required Telegram chat setting {names[0]}."
        )
    try:
        return int(value)
    except ValueError:
        raise config.ConfigurationError(
            f"{names[0]} must be a numeric Telegram chat ID."
        ) from None


async def _live_asset_map(
    broker: PocketOptionBroker,
) -> dict[str, object]:
    rows = await broker.active_assets()
    try:
        payouts = await broker.payouts()
    except Exception as exc:
        logger.warning("Payouts are unavailable (%s); continuing without them.", type(exc).__name__)
        payouts = {}
    return match_runtime_assets(rows, payouts)


def _tick_values(tick: dict[str, object]) -> tuple[datetime, float]:
    timestamp = datetime.fromtimestamp(
        _normalize_broker_timestamp(tick.get("timestamp", tick.get("time"))),
        timezone.utc,
    )
    price = float(tick.get("close", tick.get("price")))
    return timestamp, price


async def run_watch(*, seconds: int = 60) -> bool:
    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        matches = await _live_asset_map(broker)
        asset = next(
            (matches[item.id] for item in CATALOG if item.id in matches and matches[item.id].is_open),
            None,
        )
        if asset is None:
            print("No catalogue asset is available right now.")
            return False
        builder = CandleBuilder(period_seconds=STRATEGY_SETTINGS.timeframe_seconds)
        try:
            history = await broker.candle_history(
                asset.symbol,
                STRATEGY_SETTINGS.timeframe_seconds,
                STRATEGY_SETTINGS.timeframe_seconds * config.MAX_CANDLES_PER_PAIR,
            )
            builder.seed(history)
        except Exception as exc:
            logger.warning("Candle history unavailable (%s); watching live ticks.", type(exc).__name__)
        print(f"Watching {asset.display_name} ({asset.symbol}) on 1-minute candles.")
        stream = broker.subscribe_ticks(asset.symbol)
        deadline = asyncio.get_running_loop().time() + seconds
        try:
            async for tick in stream:
                closed = builder.update(tick)
                if closed is not None:
                    _print_candle(asset.display_name, closed)
                if asyncio.get_running_loop().time() >= deadline:
                    break
        finally:
            await stream.aclose()
        return True
    finally:
        await broker.close()


def _print_candle(asset_name: str, candle: Candle) -> None:
    closed_at = datetime.fromtimestamp(
        candle.time + STRATEGY_SETTINGS.timeframe_seconds, timezone.utc
    ).isoformat(timespec="seconds")
    print(
        f"{closed_at} {asset_name} "
        f"O={candle.open:g} H={candle.high:g} L={candle.low:g} C={candle.close:g}",
        flush=True,
    )


async def run_feed_check(*, seconds: int = 30) -> bool:
    return await run_watch(seconds=seconds)


async def run_pricecheck(asset_ids: Sequence[str] | None = None) -> bool:
    targets = list(asset_ids or ("aed-cny", "gbp-usd"))
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise config.ConfigurationError(
            "Missing required secret TELEGRAM_BOT_TOKEN for --run pricecheck."
        )
    admin_chat = _numeric_chat_id("TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_ID")
    bot = TelegramBotClient(bot_token)
    broker = PocketOptionBroker(config.load_ssid())
    rows: list[str] = []
    all_ok = True
    try:
        await broker.connect()
        matches = await _live_asset_map(broker)
        for asset_id in targets:
            catalog_asset = CATALOG_BY_ID.get(asset_id)
            asset = matches.get(asset_id)
            if catalog_asset is None or asset is None or not asset.is_open:
                name = catalog_asset.name if catalog_asset else asset_id
                rows.append(f"{name}: unavailable")
                all_ok = False
                continue
            stream = broker.subscribe_ticks(asset.symbol)
            try:
                tick = await asyncio.wait_for(
                    anext(stream), timeout=config.WATCHDOG_GLOBAL_STALL_SECONDS
                )
                timestamp, price = _tick_values(tick)
                rows.append(
                    f"{asset.display_name}: {price:g} at "
                    f"{timestamp.isoformat(timespec='seconds')}"
                )
            except Exception as exc:
                logger.warning(
                    "Price check failed for %s (%s).",
                    asset.display_name,
                    type(exc).__name__,
                )
                rows.append(f"{asset.display_name}: unavailable")
                all_ok = False
            finally:
                await stream.aclose()
        message = "GBot price check (UTC)\n" + "\n".join(rows)
        await bot.send_message(admin_chat, message, parse_mode=None)
        print(message, flush=True)
        return all_ok
    finally:
        await broker.close()


async def run_telegram_setup() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise config.ConfigurationError(
            "Missing required secret TELEGRAM_BOT_TOKEN for --run telegram-setup."
        )
    client = TelegramBotClient(token)
    updates = await client.get_updates(offset=None, poll_timeout=0)
    chats: set[int] = set()
    for update in updates:
        message = update.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict) and chat.get("id") is not None:
                try:
                    chats.add(int(chat["id"]))
                except (TypeError, ValueError):
                    pass
    if not chats:
        print("No Telegram chats were returned.")
        return False
    for chat_id in sorted(chats):
        print(f"Telegram chat ID: {chat_id}")
    return True


async def run_notify_test() -> bool:
    notifier = build_notifier()
    if not isinstance(notifier, TelegramNotifier):
        print("NOTIFY TEST NOT RUN: Telegram settings are missing.")
        return False
    await notifier.send_text(
        "GBot notification test. No market signal was generated and no trade was placed."
    )
    print("NOTIFY TEST SENT")
    return True


async def run_test_signal() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise config.ConfigurationError(
            "Missing required secret TELEGRAM_BOT_TOKEN for --run testsignal."
        )
    admin_chat = _numeric_chat_id("TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_ID")
    bot = TelegramBotClient(token)
    await bot.send_message(
        admin_chat,
        "🧪 TESTSIGNAL — not a confirmed signal. No trade was placed.",
        parse_mode=None,
    )
    print("TESTSIGNAL SENT: sample-only message delivered to the admin chat.")
    return True


def _test_signal_messages() -> list[str]:
    return [
        "🧪 TESTSIGNAL — not a confirmed signal.\n"
        "This is a delivery check only; no trade was placed."
    ]


async def run_serve() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise config.ConfigurationError(
            "Missing required secret TELEGRAM_BOT_TOKEN for --run serve."
        )
    admin_value = (
        os.environ.get("TELEGRAM_ADMIN_ID")
        or os.environ.get("TELEGRAM_USER_ID", "")
    ).strip()
    if not admin_value:
        raise config.ConfigurationError(
            "Missing required secret TELEGRAM_ADMIN_ID for admin commands."
        )
    try:
        admin_id = int(admin_value)
    except ValueError:
        raise config.ConfigurationError(
            "TELEGRAM_ADMIN_ID must be a numeric Telegram user ID."
        ) from None
    admin_chat_value = (
        os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or admin_value
    ).strip()
    try:
        admin_chat_id = int(admin_chat_value)
    except ValueError:
        raise config.ConfigurationError(
            "TELEGRAM_ADMIN_CHAT_ID must be a numeric Telegram chat ID."
        ) from None
    try:
        port = int(os.environ.get("PORT", "5000"))
    except ValueError:
        raise config.ConfigurationError("PORT must be a valid TCP port number.") from None
    if not 1 <= port <= 65535:
        raise config.ConfigurationError("PORT must be between 1 and 65535.")

    bot = TelegramBotClient(token)
    try:
        await bot.register_commands(admin_id)
    except TelegramSendError as exc:
        logger.warning(
            "Telegram command registration failed (%s); polling will still start.",
            exc.status_code or type(exc).__name__,
        )
    subscribers = await asyncio.to_thread(SubscriberStore)
    fanout = SubscriberFanoutNotifier(bot, subscribers)
    queue = NotificationQueue(fanout, max_age_seconds=10)
    outcomes = SignalOutcomeTracker("data/signals.csv")

    def record_signal(signal: Signal) -> None:
        outcomes.record_signal(signal)
        print(
            f"{signal.time.astimezone(timezone.utc).isoformat(timespec='seconds')} "
            f"{signal.asset_name} {signal.setup} {signal.direction} "
            f"entry={signal.price:g}",
            flush=True,
        )
        queue.enqueue(signal)

    scanner = SignalScanner(
        PocketOptionBroker(config.load_ssid()),
        subscribers,
        on_signal=record_signal,
        on_tick=outcomes.on_live_tick,
        on_candle=lambda pair, candle: outcomes.on_closed_candle(
            pair, candle, STRATEGY_SETTINGS.timeframe_seconds
        ),
    )
    handler = TelegramCommandHandler(
        bot,
        subscribers,
        admin_id=admin_id,
        referral_link=os.environ.get("REFERRAL_LINK"),
        scanner=scanner,
        fanout=fanout,
        admin_chat_id=admin_chat_id,
        test_signal_provider=_test_signal_messages,
    )
    polling = TelegramPollingService(bot, handler)
    stop_event = asyncio.Event()
    service_state = {"scanner_running": False}

    def public_health() -> dict[str, object]:
        if scanner.is_paused:
            state = "paused"
        elif service_state["scanner_running"]:
            state = "running"
        else:
            state = "starting"
        return {
            "ok": True,
            "scanner": state,
            "feed_connected": scanner.broker.is_connected,
            "pairs_scanning": len(scanner.pairs),
        }

    def admin_status() -> dict[str, object]:
        return {
            **public_health(),
            **handler.admin_metrics(),
            "active_pairs": len(scanner.pairs),
            "active_signals": len(scanner.active_signals),
            "ssid_configured": bool(os.environ.get("PO_SSID", "").strip()),
        }

    status_server = AdminHttpServer(
        health_provider=public_health,
        admin_provider=admin_status,
        admin_token=os.environ.get("ADMIN_TOKEN"),
        port=port,
    )
    await status_server.start()

    async def scan_forever() -> None:
        service_state["scanner_running"] = True
        try:
            await scanner.run()
        finally:
            service_state["scanner_running"] = False

    queue.start()
    scan_task = asyncio.create_task(scan_forever(), name="gbot-selected-asset-scanner")
    poll_task = asyncio.create_task(polling.run(stop_event), name="gbot-telegram-polling")
    http_task = asyncio.create_task(status_server.serve_forever(), name="gbot-health-http")
    try:
        await asyncio.wait_for(
            scanner.wait_until_ready(),
            timeout=config.INITIAL_CONNECT_TIMEOUT_SECONDS + 10,
        )
        database_label = "postgres" if subscribers.backend == "postgres" else "file"
        message = (
            f"GBot started. Feed {'OK' if scanner.broker.is_connected else 'NOT OK'}, "
            f"{len(scanner.pairs)} selected assets, {subscribers.count()} subscribers, "
            f"database {database_label}."
        )
        try:
            await bot.send_message(admin_chat_id, message, parse_mode=None)
        except TelegramSendError as exc:
            logger.warning(
                "Startup status message could not be sent (%s).",
                exc.status_code or type(exc).__name__,
            )
        done, _pending = await asyncio.wait(
            {scan_task, poll_task, http_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise error
    finally:
        stop_event.set()
        for task in (scan_task, poll_task, http_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(scan_task, poll_task, http_task, return_exceptions=True)
        await status_server.close()
        await queue.close()
        outcomes.print_summary()


async def run_replay() -> int:
    from strategy.replay import run_catalog_replay

    return await run_catalog_replay()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option price-action scanner."
    )
    parser.add_argument(
        "--run",
        choices=(
            "serve",
            "pricecheck",
            "replay",
            "watch",
            "feed-check",
            "notify-test",
            "telegram-setup",
            "testsignal",
        ),
        default="watch",
    )
    parser.add_argument("--pair", action="append", help="Catalogue asset ID for pricecheck.")
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.run == "serve":
            asyncio.run(run_serve())
        elif args.run == "pricecheck":
            return 0 if asyncio.run(run_pricecheck(args.pair)) else 1
        elif args.run == "replay":
            return asyncio.run(run_replay())
        elif args.run == "watch":
            return 0 if asyncio.run(run_watch(seconds=args.seconds)) else 1
        elif args.run == "feed-check":
            return 0 if asyncio.run(run_feed_check(seconds=args.seconds)) else 1
        elif args.run == "notify-test":
            return 0 if asyncio.run(run_notify_test()) else 1
        elif args.run == "telegram-setup":
            return 0 if asyncio.run(run_telegram_setup()) else 1
        elif args.run == "testsignal":
            return 0 if asyncio.run(run_test_signal()) else 1
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        return 130
    except (
        config.ConfigurationError,
        PocketOptionAuthenticationError,
        PocketOptionConnectionError,
        PocketOptionBrokerError,
        CandleDataError,
        ValueError,
    ) as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.error("GBot stopped unexpectedly (%s).", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

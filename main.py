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
from autotrade_client import AutotradeClient, AutotradeRoster
from autotrade_commands import AUTOTRADE_ADMIN_COMMANDS, AUTOTRADE_COMMANDS
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


def _subscribed_symbols(subscribers: SubscriberStore) -> set[str]:
    return {
        subscriber.selected_asset
        for subscriber in subscribers.subscribers.values()
        if subscriber.subscribed and subscriber.selected_asset
    }


async def _read_selected_tick(
    broker: PocketOptionBroker, symbol: str
) -> tuple[datetime, float, bool] | None:
    """Read one fresh tick only for a symbol selected by an active chat."""
    stream = broker.subscribe_ticks(symbol)
    deadline = asyncio.get_running_loop().time() + 5.0
    last_tick: tuple[datetime, float, bool] | None = None
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return last_tick
            try:
                tick = await asyncio.wait_for(anext(stream), timeout=remaining)
            except (StopAsyncIteration, TimeoutError):
                return last_tick
            try:
                timestamp, price = _tick_values(tick)
            except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
                continue
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            last_tick = (timestamp, price, 0 <= age < 5.0)
            if last_tick[2]:
                return last_tick
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Asset tick check failed (%s).", type(exc).__name__)
        return last_tick
    finally:
        try:
            await stream.aclose()
        except Exception:
            pass


async def _selected_tick_snapshots(
    broker: PocketOptionBroker, symbols: set[str]
) -> dict[str, tuple[datetime, float, bool]]:
    ordered_symbols = sorted(symbols)
    if not ordered_symbols:
        return {}
    results = await asyncio.gather(
        *(_read_selected_tick(broker, symbol) for symbol in ordered_symbols),
        return_exceptions=True,
    )
    return {
        symbol: result
        for symbol, result in zip(ordered_symbols, results)
        if isinstance(result, tuple)
    }


async def run_assets() -> bool:
    """Print the live catalogue; tick probes are limited to selected symbols."""
    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        matches = await _live_asset_map(broker)
        subscribers = await asyncio.to_thread(SubscriberStore)
        selected = _subscribed_symbols(subscribers)
        snapshots = await _selected_tick_snapshots(broker, selected)
        for category in dict.fromkeys(asset.category for asset in CATALOG):
            print(f"\n{category}")
            print("Display name | Symbol | Payout | Open | Tick status")
            for asset in CATALOG:
                if asset.category != category:
                    continue
                live = matches.get(asset.id)
                if live is None:
                    symbol, payout, is_open, tick_status = (
                        "unmatched",
                        "unavailable",
                        "unavailable",
                        "not mapped",
                    )
                else:
                    symbol = live.symbol
                    payout = (
                        f"{live.payout:g}%"
                        if live.payout is not None
                        else "unavailable"
                    )
                    is_open = "yes" if live.is_open else "no"
                    snapshot = snapshots.get(symbol) if symbol in selected else None
                    if snapshot is None:
                        tick_status = (
                            "no fresh tick" if symbol in selected else "not selected"
                        )
                    else:
                        tick_status = "fresh" if snapshot[2] else "stale"
                print(
                    f"{asset.name} | {symbol} | {payout} | {is_open} | {tick_status}"
                )
        return True
    finally:
        await broker.close()


async def run_availability(*, wait_seconds: float = 10.0) -> bool:
    """Per catalogue asset: mapped symbol, live-list presence, subscribe, fresh tick."""
    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        rows = await broker.active_assets()
        live_symbols = {row.get("symbol") for row in rows}
        matches = await _live_asset_map(broker)
        targets = sorted(
            {live.symbol for live in matches.values() if live.is_open}
        )
        subscribed: dict[str, bool] = {}
        last_tick: dict[str, float] = {}

        async def watch(symbol: str) -> None:
            stream = broker.subscribe_ticks(symbol)
            try:
                async for tick in stream:
                    subscribed[symbol] = True
                    try:
                        timestamp, _price = _tick_values(tick)
                    except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
                        continue
                    last_tick[symbol] = timestamp.timestamp()
            except Exception as exc:
                logger.warning("Availability stream failed (%s).", type(exc).__name__)
            finally:
                await stream.aclose()

        async def subscribe_all() -> None:
            for symbol in targets:
                subscribed.setdefault(symbol, False)
                tasks.append(asyncio.create_task(watch(symbol)))
                await asyncio.sleep(0.2)

        tasks: list[asyncio.Task[None]] = []
        await subscribe_all()
        await asyncio.sleep(wait_seconds)
        now = datetime.now(timezone.utc).timestamp()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("Display name | Symbol | In live list | Subscribed | Tick in last 10s")
        ticking = 0
        for asset in CATALOG:
            live = matches.get(asset.id)
            symbol = live.symbol if live else "unmatched"
            in_list = "yes" if live and live.symbol in live_symbols else "no"
            sub = "yes" if live and live.symbol in subscribed else "no"
            fresh = live is not None and now - last_tick.get(live.symbol, 0) <= 10
            ticking += int(fresh)
            print(f"{asset.name} | {symbol} | {in_list} | {sub} | {'yes' if fresh else 'no'}")
        print(f"Assets with a tick in the last 10s: {ticking}/{len(CATALOG)}")
        return ticking > 0
    finally:
        await broker.close()


async def run_payouts() -> bool:
    """Print each catalogue asset with its payout as read from the feed now."""
    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        matches = match_runtime_assets(await broker.active_assets())
        payouts = await broker.payouts()
        read_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"Account: {config.account_type()} | read at {read_at}")
        print("Display name | Symbol | Feed payout")
        known = 0
        for asset in CATALOG:
            live = matches.get(asset.id)
            value = payouts.get(live.symbol) if live else None
            if value is not None and not 0 < value <= 100:
                value = None
            known += int(value is not None)
            print(
                f"{asset.name} | {live.symbol if live else 'unmatched'} | "
                + (f"{value:g}%" if value is not None else "unavailable")
            )
        print(config.PAYOUT_DISCLAIMER)
        return known > 0
    finally:
        await broker.close()


async def run_candles(asset_name: str) -> bool:
    """Print the last 3 closed 1-minute candles and the next close time."""
    catalog_asset = next(
        (a for a in CATALOG if a.name == asset_name or a.id == asset_name), None
    )
    if catalog_asset is None:
        print(f"Unknown catalogue asset: {asset_name}")
        return False
    period = STRATEGY_SETTINGS.timeframe_seconds
    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        live = match_runtime_assets(await broker.active_assets()).get(catalog_asset.id)
        if live is None:
            print(f"{catalog_asset.name}: no live symbol")
            return False
        builder = CandleBuilder(period_seconds=period, max_candles=10)
        builder.seed(await broker.candle_history(live.symbol, period, period * 10))
        closed = list(builder.closed)[-3:]
        from telegram_bot import configured_timezone

        local_zone = configured_timezone()
        print(f"{catalog_asset.name} ({live.symbol}), {period}s candles")
        for candle in closed:
            start = datetime.fromtimestamp(candle.time, timezone.utc)
            print(
                f"start={start.isoformat(timespec='seconds')} "
                f"({start.astimezone(local_zone).strftime('%H:%M %Z')}) "
                f"O={candle.open:g} H={candle.high:g} L={candle.low:g} C={candle.close:g}"
            )
        now = datetime.now(timezone.utc).timestamp()
        next_close = (int(now) // period + 1) * period
        print(
            "Next close: "
            + datetime.fromtimestamp(next_close, timezone.utc).isoformat(timespec="seconds")
        )
        latest_closed_start = next_close - 2 * period
        if closed and closed[-1].time < latest_closed_start:
            behind = (latest_closed_start - closed[-1].time) // period
            print(
                f"Note: feed history is {behind} closed candle(s) behind the clock; "
                "the live stream fills these after an asset is selected."
            )
        return len(closed) == 3
    finally:
        await broker.close()


async def run_asset_check(asset_name: str) -> bool:
    """Read-only: map the name, subscribe that symbol (2 x 10 s), report tick and payout."""
    catalog_asset = next(
        (
            asset
            for asset in CATALOG
            if asset.name == asset_name or asset.id == asset_name
        ),
        None,
    )
    print(f"Display name: {catalog_asset.name if catalog_asset else asset_name}")
    if catalog_asset is None:
        print("Mapping found: no (not in catalogue)")
        return False

    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        rows = await broker.active_assets()
        live = match_runtime_assets(rows).get(catalog_asset.id)
        print(f"Catalogue symbol: {catalog_asset.symbol or 'none'}")
        print(f"Mapped symbol: {live.symbol if live else 'unavailable'}")
        print(f"In broker live list: {'yes' if live else 'no'}")
        print(f"Open: {'yes' if live and live.is_open else 'no'}")
        tick: tuple[datetime, float] | None = None
        attempts = 0
        if live and live.is_open:
            for attempts in range(1, 3):
                stream = broker.subscribe_ticks(live.symbol)
                try:
                    deadline = asyncio.get_running_loop().time() + 10.0
                    while tick is None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(anext(stream), timeout=remaining)
                        except (StopAsyncIteration, TimeoutError):
                            break
                        try:
                            stamp, price = _tick_values(raw)
                        except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
                            continue
                        age = (datetime.now(timezone.utc) - stamp).total_seconds()
                        if 0 <= age < 10:
                            tick = (stamp, price)
                finally:
                    await stream.aclose()
                if tick is not None:
                    break
        print(f"Subscribe attempts: {attempts}")
        print(f"Subscribed: {'yes' if attempts else 'no'}")
        print(
            "First tick time: "
            + (tick[0].isoformat(timespec="seconds") if tick else "none within 10 s")
        )
        print(f"Last price: {tick[1]:g}" if tick else "Last price: unavailable")
        payout = await broker.payout(live.symbol) if live else None
        print(
            (f"Payout: {payout:g}%" if payout is not None else "Payout: unavailable")
            + f" ({config.account_type()} account)"
        )
        print(f"Result: {'available' if tick else 'unavailable'}")
        return tick is not None
    finally:
        await broker.close()


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
    admin_value = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
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
        # None unless AUTOTRADE_API_URL and AUTOTRADE_JWT_SECRET are set.
        autotrade = AutotradeClient.from_env()
    except ValueError as exc:
        raise config.ConfigurationError(str(exc)) from None
    try:
        if autotrade is None:
            await bot.register_commands(admin_id)
        else:
            await bot.register_commands(
                admin_id,
                extra_public=AUTOTRADE_COMMANDS,
                extra_admin=AUTOTRADE_ADMIN_COMMANDS,
            )
    except TelegramSendError as exc:
        logger.warning(
            "Telegram command registration failed (%s); polling will still start.",
            exc.status_code or type(exc).__name__,
        )
    subscribers = await asyncio.to_thread(SubscriberStore)
    # Shared "who is auto-trading" list: those users get trades + trade alerts instead of signal cards.
    roster = AutotradeRoster(autotrade) if autotrade is not None else None
    if roster is not None:
        await roster.refresh()
    fanout = SubscriberFanoutNotifier(bot, subscribers, autotrade=autotrade, roster=roster)
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
        background_selection=True,
        autotrade=autotrade,
        roster=roster,
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
            "account_type": config.account_type(),
        }

    status_server = AdminHttpServer(
        health_provider=public_health,
        admin_provider=admin_status,
        admin_token=os.environ.get("ADMIN_TOKEN"),
        host="0.0.0.0",
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
        unmatched_names = getattr(scanner, "unmatched_catalog_names", [])
        if unmatched_names:
            message += "\nCatalogue names not matched: " + ", ".join(unmatched_names)
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


async def run_strategy_debug(asset_id: str) -> bool:
    """Print safe strategy state from the latest closed history for one catalogue asset."""
    catalog_asset = CATALOG_BY_ID.get(asset_id)
    if catalog_asset is None:
        print("Unknown catalogue asset ID.")
        return False

    broker = PocketOptionBroker(config.load_ssid())
    try:
        await broker.connect()
        matches = match_runtime_assets(await broker.active_assets())
        asset = matches.get(asset_id)
        if asset is None or not asset.is_open:
            print(f"STRATEGY DEBUG asset={catalog_asset.name} unavailable")
            return False

        history = await broker.candle_history(
            asset.symbol,
            STRATEGY_SETTINGS.timeframe_seconds,
            60 * STRATEGY_SETTINGS.timeframe_seconds,
        )
        builder = CandleBuilder(
            period_seconds=STRATEGY_SETTINGS.timeframe_seconds,
            max_candles=60,
        )
        builder.seed(history)
        from strategy.price_action import PriceActionStrategy

        strategy = PriceActionStrategy()
        strategy.bootstrap(tuple(builder.closed))
        print(strategy.format_debug_state(asset.display_name), flush=True)
        return len(builder.closed) > 0
    except Exception as exc:
        # Provider exception text can contain request details; diagnostics need only its type.
        logger.warning("Strategy debug failed (%s).", type(exc).__name__)
        print(f"STRATEGY DEBUG unavailable ({type(exc).__name__})")
        return False
    finally:
        await broker.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Pocket Option price-action scanner."
    )
    parser.add_argument(
        "--run",
        choices=(
            "serve",
            "assets",
            "asset-check",
            "availability",
            "payouts",
            "candles",
            "pricecheck",
            "strategy-debug",
            "replay",
            "watch",
            "feed-check",
            "notify-test",
            "telegram-setup",
            "testsignal",
        ),
        default="watch",
    )
    parser.add_argument(
        "--pair",
        action="append",
        help="Catalogue asset ID for pricecheck or strategy-debug.",
    )
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument(
        "asset_name",
        nargs="?",
        help="Exact catalogue display name for --run asset-check or candles.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.run == "serve":
            asyncio.run(run_serve())
        elif args.run == "assets":
            return 0 if asyncio.run(run_assets()) else 1
        elif args.run == "candles":
            if not args.asset_name:
                parser.error("--run candles requires an exact asset display name.")
            return 0 if asyncio.run(run_candles(args.asset_name)) else 1
        elif args.run == "payouts":
            return 0 if asyncio.run(run_payouts()) else 1
        elif args.run == "availability":
            return 0 if asyncio.run(run_availability()) else 1
        elif args.run == "asset-check":
            if not args.asset_name:
                parser.error("--run asset-check requires an exact asset display name.")
            return 0 if asyncio.run(run_asset_check(args.asset_name)) else 1
        elif args.run == "pricecheck":
            return 0 if asyncio.run(run_pricecheck(args.pair)) else 1
        elif args.run == "strategy-debug":
            if not args.pair or len(args.pair) != 1:
                parser.error("--run strategy-debug requires one --pair catalogue asset ID.")
            return 0 if asyncio.run(run_strategy_debug(args.pair[0])) else 1
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

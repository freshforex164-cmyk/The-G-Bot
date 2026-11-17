"""Pluggable, asynchronous signal notification backends."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Protocol

from strategy.model import Signal
from strategy.price_action import SETUP_LABELS

logger = logging.getLogger("gbot.notifications")


def format_signal_message(signal: Signal) -> str:
    direction = html.escape(signal.direction.upper(), quote=False)
    icon = "🔴" if direction == "SELL" else "🟢"
    asset_name = html.escape(signal.asset_name, quote=False)
    setup = html.escape(SETUP_LABELS[signal.setup], quote=False)
    payout = f"\nPayout: {signal.payout:g}%" if signal.payout is not None else ""
    return (
        f"{icon} <b>{direction}</b>\n"
        f"Asset: {asset_name}\n"
        f"Setup: {setup}\n"
        f"Expiry: 1 minute{payout}\n"
        "Valid for: 10 seconds"
    )


class Notifier(Protocol):
    async def send(self, signal: Signal) -> str: ...


class ConsoleNotifier:
    async def send(self, signal: Signal) -> str:
        print(format_signal_message(signal), flush=True)
        return "printed to console"


class TelegramSendError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class TelegramNotifier:
    """Send plain-text signal notifications through Telegram's HTTPS Bot API."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 5.0,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    async def send(self, signal: Signal) -> str:
        return await self.send_text(format_signal_message(signal))

    async def send_text(self, message: str) -> str:
        await asyncio.to_thread(self._send_sync, message)
        return "sent through Telegram"

    def _send_sync(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            with response:  # type: ignore[attr-defined]
                status = int(
                    getattr(response, "status", response.getcode())  # type: ignore[attr-defined]
                )
                body = response.read()  # type: ignore[attr-defined]
            self._raise_api_error(status, body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            self._raise_api_error(exc.code, body)
        except urllib.error.URLError as exc:
            reason_type = type(exc.reason).__name__
            raise TelegramSendError(
                f"Telegram request failed ({reason_type})."
            ) from None
        except TelegramSendError:
            raise
        except Exception as exc:
            raise TelegramSendError(
                f"Telegram request failed ({type(exc).__name__})."
            ) from None

    @staticmethod
    def _raise_api_error(status: int, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if 200 <= status < 300 and payload.get("ok", True):
            return

        description = str(payload.get("description", "")).lower()
        if status == 401:
            message = "Telegram rejected the request: invalid token (401)."
        elif status == 400 and "chat not found" in description:
            message = (
                "Telegram could not find this chat (400): TELEGRAM_CHAT_ID may "
                "be wrong, or the bot has not been added to that chat/channel. "
                "Use telegram-setup to check the returned chat ID, then add the "
                "bot to the chat."
            )
        elif status == 400:
            message = "Telegram rejected the message (400)."
        elif status == 403:
            message = (
                "Telegram denied the request (403): the bot may be blocked or "
                "not a member; for a channel, add it and grant admin posting rights."
            )
        elif status == 429:
            parameters = payload.get("parameters", {})
            retry_after = (
                parameters.get("retry_after")
                if isinstance(parameters, Mapping)
                else None
            )
            try:
                seconds = max(0, int(retry_after))
            except (TypeError, ValueError):
                seconds = None
            if seconds is None:
                raise TelegramSendError(
                    "Telegram rate limited the request (429); wait for "
                    "retry_after before sending again."
                )
            raise TelegramSendError(
                f"Telegram rate limited the request (429); wait {seconds}s "
                "before sending again.",
                retry_after=seconds,
                status_code=status,
            )
        else:
            message = f"Telegram send failed (HTTP {status})."
        raise TelegramSendError(message, status_code=status)


async def telegram_get_updates(
    token: str,
    *,
    timeout: float = 5.0,
    opener: Callable[..., object] | None = None,
) -> list[Mapping[str, object]]:
    """Read Telegram updates without ever including the token in errors."""
    if not token.strip():
        raise TelegramSendError("Missing required secret TELEGRAM_BOT_TOKEN.")
    open_request = opener or urllib.request.urlopen

    def fetch() -> list[Mapping[str, object]]:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getUpdates",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            response = open_request(request, timeout=timeout)
            with response:  # type: ignore[attr-defined]
                status = int(
                    getattr(response, "status", response.getcode())  # type: ignore[attr-defined]
                )
                body = response.read()  # type: ignore[attr-defined]
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            TelegramNotifier._raise_api_error(exc.code, body)
            raise AssertionError("unreachable")
        except urllib.error.URLError as exc:
            raise TelegramSendError(
                f"Telegram getUpdates request failed ({type(exc.reason).__name__})."
            ) from None
        except TelegramSendError:
            raise
        except Exception as exc:
            raise TelegramSendError(
                f"Telegram getUpdates request failed ({type(exc).__name__})."
            ) from None

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramSendError("Telegram returned an unreadable getUpdates response.")
        if not 200 <= status < 300 or not payload.get("ok"):
            TelegramNotifier._raise_api_error(status, body)
        updates = payload.get("result", [])
        if not isinstance(updates, list):
            raise TelegramSendError("Telegram returned an unexpected getUpdates response.")
        return [item for item in updates if isinstance(item, Mapping)]

    return await asyncio.to_thread(fetch)


def build_notifier(
    environ: Mapping[str, str] | None = None,
) -> Notifier:
    environment = os.environ if environ is None else environ
    token = environment.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = environment.get("TELEGRAM_CHAT_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "Telegram notifications disabled: missing %s; continuing with "
            "console notifications.",
            " and ".join(missing),
        )
        return ConsoleNotifier()
    return TelegramNotifier(token, chat_id)


class NotificationQueue:
    """Nonblocking queue that drops signals after the 10-second send window."""

    def __init__(
        self,
        notifier: Notifier,
        *,
        max_age_seconds: int = 10,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be greater than zero")
        self.notifier = notifier
        self.max_age_seconds = max_age_seconds
        self._pending: list[Signal] = []
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(), name="signal-notification-queue"
            )

    def enqueue(self, signal: Signal) -> None:
        if self._closing:
            return
        if self._is_stale(signal, datetime.now(timezone.utc)):
            logger.info("Expired queued notification was dropped.")
            return
        self._pending.append(signal)
        self._wake.set()

    def _is_stale(self, signal: Signal, now: datetime) -> bool:
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        return (
            (now - signal_time).total_seconds() > self.max_age_seconds
            or (
                signal.valid_until is not None
                and signal.valid_until <= now
            )
        )

    async def _wait_for_wake(self, timeout: float | None = None) -> None:
        self._wake.clear()
        if timeout is None:
            await self._wake.wait()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.01, timeout))
        except TimeoutError:
            pass

    async def _run(self) -> None:
        while self._pending or not self._closing:
            if not self._pending:
                await self._wait_for_wake()
                continue
            signal = self._pending.pop(0)
            if self._is_stale(signal, datetime.now(timezone.utc)):
                logger.info("Expired queued notification was dropped.")
                continue
            try:
                await self.notifier.send(signal)
            except TelegramSendError as exc:
                logger.error("%s", exc)
            except Exception as exc:
                logger.error("Notification failed (%s).", type(exc).__name__)

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._worker is not None:
            await self._worker
            self._worker = None

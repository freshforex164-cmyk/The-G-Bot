"""Async Pocket Option adapter built on BinaryOptionsToolsV2."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, TypeVar

from BinaryOptionsToolsV2.config import Config as LibraryConfig
from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

from config import (
    CONNECTION_MONITOR_INTERVAL_SECONDS,
    INITIAL_CONNECT_TIMEOUT_SECONDS,
    RECONNECT_BASE_DELAY_SECONDS,
    RECONNECT_CALL_TIMEOUT_SECONDS,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_MAX_DELAY_SECONDS,
)

logger = logging.getLogger("g_bots.broker.pocket_option")

T = TypeVar("T")
BrokerCall = Callable[[PocketOptionAsync], Awaitable[T]]


class PocketOptionBrokerError(RuntimeError):
    """Base error for Pocket Option operations."""


class PocketOptionAuthenticationError(PocketOptionBrokerError):
    """Raised when the demo SSID is malformed, expired, or not demo-only."""


class PocketOptionConnectionError(PocketOptionBrokerError):
    """Raised when Pocket Option cannot be reached after retrying."""


class PocketOptionOperationError(PocketOptionBrokerError):
    """Raised when a broker operation fails while the connection is healthy."""


class CandleHistoryUnavailable(PocketOptionBrokerError):
    """Raised when optional historical candle seeding is unavailable."""


class PocketOptionBroker:
    """Small async adapter for the documented BinaryOptionsToolsV2 API."""

    def __init__(self, ssid: str) -> None:
        self._ssid = ssid
        self._client: PocketOptionAsync | None = None
        self._closed = False
        self._disconnected = False
        self._demo_confirmed = False
        self._connection_generation = 0
        self._reconnect_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(self._client.is_connected())
        except Exception:
            return False

    async def connect(self) -> None:
        """Create the client, wait for its asset list, and require a demo session."""
        if self._closed:
            raise PocketOptionBrokerError("This broker client has already been closed.")

        if self._client is not None:
            if self.is_connected:
                self._require_demo_account()
                return
            await self._mark_disconnected("connection was unavailable during connect")
            await self._reconnect_with_backoff()
            return

        # The library connects during construction. Terminal logging is disabled
        # so provider diagnostics cannot accidentally expose authentication data.
        library_config = LibraryConfig(
            terminal_logging=False,
            connection_initialization_timeout_secs=int(
                INITIAL_CONNECT_TIMEOUT_SECONDS
            ),
            timeout_secs=int(RECONNECT_CALL_TIMEOUT_SECONDS),
        )
        try:
            client = PocketOptionAsync(ssid=self._ssid, config=library_config)
        except Exception as exc:
            raise PocketOptionAuthenticationError(
                "Pocket Option could not initialize the demo session "
                f"({type(exc).__name__}). Verify PO_SSID_DEMO is the full, "
                "current demo SSID."
            ) from None

        self._client = client
        if not client.is_ssid_valid():
            await self._shutdown_client()
            raise PocketOptionAuthenticationError(
                "PO_SSID_DEMO has an invalid SSID format. Replace it with the "
                "complete current demo SSID beginning with 42[\"auth\",...]."
            )

        try:
            await asyncio.wait_for(
                client.wait_for_assets(timeout=INITIAL_CONNECT_TIMEOUT_SECONDS),
                timeout=INITIAL_CONNECT_TIMEOUT_SECONDS + 2,
            )
            if not client.is_connected():
                raise TimeoutError("Pocket Option did not report a live connection.")
            self._require_demo_account()
        except asyncio.CancelledError:
            await self._shutdown_client()
            raise
        except PocketOptionAuthenticationError:
            await self._shutdown_client()
            raise
        except Exception as exc:
            await self._shutdown_client()
            raise PocketOptionAuthenticationError(
                "Pocket Option did not accept or finish connecting this demo "
                f"session ({type(exc).__name__}). PO_SSID_DEMO may be invalid "
                "or expired; copy a fresh full SSID from the demo account. "
                "If it is current, check the network and Pocket Option service."
            ) from None

        self._disconnected = False
        logger.info("Connected to Pocket Option demo account.")

    def _require_client(self) -> PocketOptionAsync:
        if self._client is None:
            raise PocketOptionBrokerError("Call connect() before using the broker.")
        return self._client

    def _require_demo_account(self) -> None:
        client = self._require_client()
        if not client.is_ssid_valid():
            raise PocketOptionAuthenticationError(
                "PO_SSID_DEMO has an invalid SSID format. Replace it with the "
                "complete current demo SSID."
            )
        if not self.is_connected:
            if self._demo_confirmed:
                return
            raise PocketOptionConnectionError(
                "Pocket Option is not connected yet; call connect() first."
            )
        try:
            is_demo = client.is_demo()
        except Exception as exc:
            raise PocketOptionAuthenticationError(
                "Pocket Option could not confirm the account type "
                f"({type(exc).__name__}). Refresh PO_SSID_DEMO from the demo account."
            ) from None
        if not is_demo:
            raise PocketOptionAuthenticationError(
                "PO_SSID_DEMO is not connected to a demo account. Refusing to "
                "continue; switch Pocket Option to demo and replace the secret."
            )
        self._demo_confirmed = True

    async def _mark_disconnected(
        self, reason: str, error: BaseException | None = None
    ) -> None:
        if self._disconnected:
            return
        suffix = f" ({type(error).__name__})" if error is not None else ""
        logger.warning("Pocket Option disconnected: %s%s.", reason, suffix)
        self._disconnected = True

    async def _reconnect_with_backoff(self) -> None:
        client = self._require_client()
        if self._closed:
            raise PocketOptionConnectionError("Broker is shutting down.")

        async with self._reconnect_lock:
            if self.is_connected:
                self._require_demo_account()
                if self._disconnected:
                    self._disconnected = False
                    self._connection_generation += 1
                    logger.info("Pocket Option connection restored.")
                return

            for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
                delay = min(
                    RECONNECT_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    RECONNECT_MAX_DELAY_SECONDS,
                )
                logger.info(
                    "Pocket Option reconnect attempt %d/%d in %.1f seconds.",
                    attempt,
                    RECONNECT_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)

                try:
                    await asyncio.wait_for(
                        client.reconnect(),
                        timeout=RECONNECT_CALL_TIMEOUT_SECONDS,
                    )
                    await asyncio.wait_for(
                        client.wait_for_assets(timeout=RECONNECT_CALL_TIMEOUT_SECONDS),
                        timeout=RECONNECT_CALL_TIMEOUT_SECONDS + 1,
                    )
                    if not self.is_connected:
                        raise TimeoutError("Connection is not ready.")
                    self._require_demo_account()
                except asyncio.CancelledError:
                    raise
                except PocketOptionAuthenticationError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Pocket Option reconnect attempt %d failed (%s).",
                        attempt,
                        type(exc).__name__,
                    )
                    continue

                self._disconnected = False
                self._connection_generation += 1
                logger.info("Reconnected to Pocket Option.")
                return

        raise PocketOptionConnectionError(
            "Could not reconnect to Pocket Option after several attempts. "
            "PO_SSID_DEMO may be expired or invalid; refresh the full demo SSID "
            "and check the network."
        )

    async def _request(self, operation: str, call: BrokerCall[T]) -> T:
        client = self._require_client()
        for attempt in range(2):
            if not self.is_connected:
                await self._mark_disconnected(f"connection lost before {operation}")
                await self._reconnect_with_backoff()

            try:
                return await call(client)
            except asyncio.CancelledError:
                raise
            except (ValueError, TypeError) as exc:
                raise PocketOptionOperationError(
                    f"Pocket Option could not {operation} ({type(exc).__name__}). "
                    "If this is an authentication failure, refresh PO_SSID_DEMO; "
                    "otherwise verify the request and Pocket Option service."
                ) from None
            except Exception as exc:
                if self.is_connected and not isinstance(
                    exc, (ConnectionError, TimeoutError, OSError)
                ):
                    try:
                        if not client.is_demo():
                            raise PocketOptionAuthenticationError(
                                "Pocket Option rejected the demo session. "
                                "PO_SSID_DEMO may be expired or invalid; refresh "
                                "the full demo SSID."
                            )
                    except PocketOptionAuthenticationError:
                        raise
                    except Exception:
                        pass
                    raise PocketOptionOperationError(
                        f"Pocket Option could not {operation} "
                        f"({type(exc).__name__}). If this is an authentication "
                        "failure, refresh PO_SSID_DEMO; otherwise verify the "
                        "request and Pocket Option service."
                    ) from None

                await self._mark_disconnected(f"connection lost during {operation}", exc)
                await self._reconnect_with_backoff()

                if attempt == 1:
                    break

        raise PocketOptionConnectionError(
            f"Pocket Option could not {operation} after reconnecting."
        )

    async def balance(self) -> float:
        self._require_demo_account()
        return await self._request("read the demo balance", lambda client: client.balance())

    async def payouts(self) -> dict[str, float]:
        self._require_demo_account()
        raw_payouts = await self._request(
            "read payouts", lambda client: client.payout()
        )
        if not isinstance(raw_payouts, dict):
            raise PocketOptionOperationError(
                "Pocket Option returned payouts in an unexpected format."
            )

        parsed: dict[str, float] = {}
        for pair, payout in raw_payouts.items():
            if isinstance(pair, str) and isinstance(payout, (int, float)):
                parsed[pair] = float(payout)
        return parsed

    async def candle_history(
        self, pair: str, period_seconds: int, lookback_seconds: int
    ) -> list[dict[str, Any]]:
        self._require_demo_account()
        try:
            history = await self._request(
                "fetch candle history",
                lambda client: client.compile_candles(
                    pair, period_seconds, lookback_seconds
                ),
            )
        except (PocketOptionAuthenticationError, PocketOptionConnectionError):
            raise
        except PocketOptionOperationError as exc:
            raise CandleHistoryUnavailable(
                f"Historical candle seed is unavailable for {pair} "
                f"({type(exc).__name__}). Live candles will start without a seed."
            ) from None

        if not isinstance(history, list):
            raise CandleHistoryUnavailable(
                f"Historical candle seed for {pair} had an unexpected format."
            )
        return history

    async def subscribe_ticks(self, pair: str) -> AsyncGenerator[dict[str, Any], None]:
        """Yield raw price updates from BinaryOptionsToolsV2.subscribe_symbol()."""
        while not self._closed:
            client = self._require_client()
            if not self.is_connected:
                await self._mark_disconnected(
                    f"connection lost before subscribing to {pair}"
                )
                await self._reconnect_with_backoff()

            subscription: Any | None = None
            next_tick: asyncio.Task[Any] | None = None
            reconnect_needed = False
            try:
                generation = self._connection_generation
                subscription = await client.subscribe_symbol(pair)
                iterator = subscription.__aiter__()
                while not self._closed:
                    if self._connection_generation != generation:
                        reconnect_needed = True
                        break
                    if not self.is_connected:
                        await self._mark_disconnected(
                            f"connection lost while streaming {pair}"
                        )
                        await self._reconnect_with_backoff()
                        reconnect_needed = True
                        break

                    if next_tick is None:
                        next_tick = asyncio.create_task(iterator.__anext__())

                    done, _pending = await asyncio.wait(
                        {next_tick},
                        timeout=CONNECTION_MONITOR_INTERVAL_SECONDS,
                    )
                    if not done:
                        continue

                    try:
                        tick = next_tick.result()
                    except StopAsyncIteration:
                        next_tick = None
                        await self._mark_disconnected(
                            f"tick stream ended for {pair}"
                        )
                        reconnect_needed = True
                        break
                    next_tick = None

                    if not isinstance(tick, dict):
                        raise PocketOptionOperationError(
                            f"Pocket Option returned a non-object tick for {pair}."
                        )
                    yield tick

                if not self._closed and not reconnect_needed:
                    reconnect_needed = True
            except asyncio.CancelledError:
                raise
            except PocketOptionAuthenticationError:
                raise
            except PocketOptionConnectionError:
                raise
            except PocketOptionOperationError:
                raise
            except ValueError as exc:
                raise PocketOptionOperationError(
                    f"Pocket Option rejected the tick subscription for {pair} "
                    f"({type(exc).__name__})."
                ) from None
            except Exception as exc:
                await self._mark_disconnected(
                    f"tick stream failed for {pair}", exc
                )
                reconnect_needed = True
            finally:
                if next_tick is not None and not next_tick.done():
                    next_tick.cancel()
                    await asyncio.gather(next_tick, return_exceptions=True)
                if subscription is not None and not self._closed:
                    try:
                        await client.unsubscribe(pair)
                    except Exception as exc:
                        logger.warning(
                            "Could not clean up the %s tick subscription (%s).",
                            pair,
                            type(exc).__name__,
                        )
            if reconnect_needed and not self._closed:
                await self._reconnect_with_backoff()

    async def monitor_connection(self, stop_event: asyncio.Event) -> None:
        """Poll connection state so reconnects performed by the library are logged."""
        while not stop_event.is_set() and not self._closed:
            if self._client is not None and not self.is_connected:
                await self._mark_disconnected("connection monitor detected a drop")
                await self._reconnect_with_backoff()

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=CONNECTION_MONITOR_INTERVAL_SECONDS,
                )
            except TimeoutError:
                pass

    async def buy(
        self, pair: str, amount: float, direction: str, expiry_seconds: int
    ) -> Any:
        """Place a demo order when called; main.py intentionally never calls this."""
        self._require_demo_account()
        if amount <= 0:
            raise ValueError("amount must be greater than zero")
        if expiry_seconds <= 0:
            raise ValueError("expiry_seconds must be greater than zero")

        normalized_direction = direction.strip().lower()
        if normalized_direction not in {"call", "put"}:
            raise ValueError("direction must be either 'call' or 'put'")

        if normalized_direction == "call":
            return await self._request(
                "place the demo call order",
                lambda client: client.buy(pair, amount, expiry_seconds),
            )
        return await self._request(
            "place the demo put order",
            lambda client: client.sell(pair, amount, expiry_seconds),
        )

    async def _shutdown_client(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.shutdown()
        except Exception as exc:
            logger.warning(
                "Pocket Option client shutdown failed (%s).", type(exc).__name__
            )

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            logger.info("Closing Pocket Option client.")
            await self._shutdown_client()

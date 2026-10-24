"""Small, token-protected HTTP status server for the long-running bot."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit
from typing import Any

StatusProvider = Callable[[], Mapping[str, Any]]

_RESPONSE_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}


class AdminHttpServer:
    """Serve a public health summary and a private operational status endpoint."""

    def __init__(
        self,
        *,
        health_provider: StatusProvider,
        admin_provider: StatusProvider,
        admin_token: str | None,
        host: str = "0.0.0.0",
        port: int = 5000,
    ) -> None:
        self.health_provider = health_provider
        self.admin_provider = admin_provider
        self.admin_token = admin_token.strip() if admin_token else None
        self.host = host
        self.port = port
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> asyncio.AbstractServer:
        if self.server is not None:
            raise RuntimeError("The status server has already been started.")
        self.server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            limit=16 * 1024,
        )
        return self.server

    async def serve_forever(self) -> None:
        if self.server is None:
            raise RuntimeError("Start the status server before serving requests.")
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        self.server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = 400
        payload: Mapping[str, Any] = {"error": "bad request"}
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if len(request_line) > 4096:
                raise ValueError("request line too large")
            parts = request_line.decode("ascii", errors="strict").strip().split()
            if len(parts) != 3:
                raise ValueError("malformed request line")
            method, target, _version = parts

            headers: dict[str, str] = {}
            for _ in range(100):
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line:
                    raise ValueError("request headers ended early")
                if line in {b"\r\n", b"\n"}:
                    break
                name, separator, value = line.decode(
                    "latin-1", errors="replace"
                ).partition(":")
                if not separator:
                    raise ValueError("malformed request header")
                headers[name.strip().lower()] = value.strip()
            else:
                raise ValueError("too many request headers")

            path = urlsplit(target).path
            if method != "GET":
                status, payload = 405, {"error": "method not allowed"}
            elif path == "/health":
                status, payload = 200, self.health_provider()
            elif path == "/admin/status":
                if self.admin_token is None:
                    status, payload = 503, {"error": "admin status is not configured"}
                elif not hmac.compare_digest(
                    headers.get("x-admin-token", ""), self.admin_token
                ):
                    status, payload = 401, {"error": "unauthorized"}
                else:
                    status, payload = 200, self.admin_provider()
            else:
                status, payload = 404, {"error": "not found"}
        except (asyncio.TimeoutError, UnicodeDecodeError, ValueError):
            status, payload = 400, {"error": "bad request"}
        except Exception:
            status, payload = 503, {"error": "status unavailable"}

        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        reason = _RESPONSE_REASONS.get(status, "Error")
        response_headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        writer.write(response_headers + body)
        try:
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

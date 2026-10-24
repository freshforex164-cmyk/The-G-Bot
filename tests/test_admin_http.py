from __future__ import annotations

import asyncio
import json
import unittest

from admin_http import AdminHttpServer


class AdminHttpServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = AdminHttpServer(
            health_provider=lambda: {"ok": True, "scanner": "running"},
            admin_provider=lambda: {
                "ok": True,
                "subscriber_count": 7,
                "ssid_configured": True,
            },
            admin_token="test-admin-secret",
            host="127.0.0.1",
            port=0,
        )
        await self.server.start()
        address = self.server.server.sockets[0].getsockname()
        self.host, self.port = address[0], address[1]

    async def asyncTearDown(self) -> None:
        await self.server.close()

    async def request(
        self, path: str, *, method: str = "GET", admin_token: str | None = None
    ) -> tuple[int, dict]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        headers = ""
        if admin_token is not None:
            headers = f"X-Admin-Token: {admin_token}\r\n"
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
            f"{headers}Connection: close\r\n\r\n".encode("ascii")
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        header, body = response.split(b"\r\n\r\n", maxsplit=1)
        status = int(header.split(b" ", maxsplit=2)[1])
        return status, json.loads(body)

    async def test_health_is_public_and_admin_status_requires_correct_token(self) -> None:
        status, payload = await self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "scanner": "running"})

        status, payload = await self.request("/admin/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "unauthorized"})

        status, _payload = await self.request(
            "/admin/status", admin_token="incorrect"
        )
        self.assertEqual(status, 401)

        status, payload = await self.request(
            "/admin/status", admin_token="test-admin-secret"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["subscriber_count"], 7)
        self.assertNotIn("database_url", payload)
        self.assertNotIn("test-admin-secret", json.dumps(payload))

    async def test_missing_admin_token_disables_private_status_endpoint(self) -> None:
        server = AdminHttpServer(
            health_provider=lambda: {"ok": True},
            admin_provider=lambda: {"secret": "private"},
            admin_token=None,
            host="127.0.0.1",
            port=0,
        )
        await server.start()
        try:
            address = server.server.sockets[0].getsockname()
            reader, writer = await asyncio.open_connection(address[0], address[1])
            writer.write(
                b"GET /admin/status HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"503 Service Unavailable", response)
            self.assertNotIn(b"private", response)
        finally:
            await server.close()

    async def test_unknown_route_and_non_get_method_are_rejected(self) -> None:
        status, _payload = await self.request("/unknown")
        self.assertEqual(status, 404)
        status, _payload = await self.request("/health", method="POST")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()

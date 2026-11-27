"""Where the sign-in browser opens Pocket Option, and which network route it uses.

Cloud servers are sometimes refused by the broker website's anti-bot edge even though the trading
WebSocket (a different host) still works. Two settings let the operator route around that without
code changes:

* ``AUTOTRADE_BROKER_WEB_URL``: an official broker website mirror, e.g. ``https://po.cash``. Only hosts in
  ``BROKER_HOSTS`` are accepted, because the hosted login form forwards credentials to this page.
* ``AUTOTRADE_BROWSER_PROXY``: a proxy used by the sign-in browser only, e.g. ``socks5://127.0.0.1:40000``
  (Cloudflare WARP proxy mode) or ``http://user:pass@host:port``. Trading connections never use it.
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

from .session_state import BROKER_HOSTS, broker_domain

DEFAULT_BROKER_WEB_URL = "https://pocketoption.com"
LOGIN_PATH = "/en/login/"
CABINET_PATHS = {"demo": "/en/cabinet/demo-quick-high-low/", "real": "/en/cabinet/quick-high-low/"}
PROXY_SCHEMES = ("http", "https", "socks5")

__all__ = ["BROKER_HOSTS", "DEFAULT_BROKER_WEB_URL", "broker_web_base", "login_url", "cabinet_urls",
           "playwright_proxy", "describe_proxy"]


def broker_web_base(raw: str | None) -> str:
    """Normalise an operator-supplied broker website origin. Raises ValueError for anything that is not
    a plain https origin on a known broker domain (no path, credentials, query or custom port)."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_BROKER_WEB_URL
    p = urlparse(value)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or p.username or p.password or p.port not in (None, 443):
        raise ValueError("AUTOTRADE_BROKER_WEB_URL must look like https://po.cash")
    if p.path not in ("", "/") or p.query or p.fragment or p.params:
        raise ValueError("AUTOTRADE_BROKER_WEB_URL must not contain a path")
    if not broker_domain(host):
        raise ValueError("AUTOTRADE_BROKER_WEB_URL host is not a known Pocket Option domain: "
                         + ", ".join(BROKER_HOSTS))
    return f"https://{host}"


def login_url(base: str = DEFAULT_BROKER_WEB_URL) -> str:
    return base.rstrip("/") + LOGIN_PATH


def cabinet_urls(base: str = DEFAULT_BROKER_WEB_URL) -> dict[str, str]:
    return {kind: base.rstrip("/") + path for kind, path in CABINET_PATHS.items()}


def playwright_proxy(raw: str | None) -> dict[str, str] | None:
    """Turn ``scheme://[user:pass@]host:port`` into Playwright's launch(proxy=...) dict (None = direct).
    Chromium cannot authenticate to SOCKS5 proxies, so SOCKS5 with credentials is rejected up front."""
    value = (raw or "").strip()
    if not value:
        return None
    p = urlparse(value)
    scheme = (p.scheme or "").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in PROXY_SCHEMES:
        raise ValueError("AUTOTRADE_BROWSER_PROXY must start with http://, https:// or socks5://")
    try:
        port = p.port
    except ValueError:
        raise ValueError("AUTOTRADE_BROWSER_PROXY has an invalid port") from None
    if not p.hostname or port is None:
        raise ValueError("AUTOTRADE_BROWSER_PROXY must include a host and port, e.g. socks5://127.0.0.1:40000")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("AUTOTRADE_BROWSER_PROXY must not contain a path")
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    proxy = {"server": f"{scheme}://{host}:{port}"}
    if p.username or p.password:
        if scheme == "socks5":
            raise ValueError("Chromium cannot use a SOCKS5 proxy with a username/password; use an http:// proxy")
        proxy["username"] = unquote(p.username or "")
        proxy["password"] = unquote(p.password or "")
    return proxy


def describe_proxy(proxy: dict[str, str] | None) -> str:
    """Log-safe description: never includes the username or password."""
    if not proxy:
        return "direct"
    return proxy["server"] + (" (with login)" if "username" in proxy else "")

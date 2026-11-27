"""Operator check: can the sign-in browser open the Pocket Option login page from this server?

    .venv/bin/python -m autotrade.check_sign_in          # the configured site and route
    .venv/bin/python -m autotrade.check_sign_in --all    # also every known official mirror

Uses the same AUTOTRADE_BROKER_WEB_URL / AUTOTRADE_BROWSER_PROXY settings and Chromium launch flags as
the real sign-in. Nothing is typed into the page and nothing is saved. Proxy credentials are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .auth.broker_web import BROKER_HOSTS, describe_proxy, login_url
from .auth.handshake import MOBILE_UA, VIEWPORT, launch_args, launch_chromium
from .config import Config
from .runtime import sign_in_network

TIMEOUT_MS = 30000


async def probe(sites: list[str], proxy: dict | None, low_memory: bool, single_process: bool,
                new_headless: bool = True) -> int:
    from playwright.async_api import async_playwright

    ok = 0
    async with async_playwright() as pw:
        launch = {"headless": True, "timeout": 60000, "args": launch_args(low_memory, single_process)}
        if proxy:
            launch["proxy"] = dict(proxy)
        browser = await launch_chromium(pw.chromium, launch, new_headless=new_headless)
        try:
            for base in sites:
                context = await browser.new_context(viewport=dict(VIEWPORT), is_mobile=True, has_touch=True,
                                                    user_agent=MOBILE_UA, locale="en-US")
                page = await context.new_page()
                started = time.monotonic()
                try:
                    response = await page.goto(login_url(base), wait_until="commit", timeout=TIMEOUT_MS)
                    status = response.status if response is not None else 0
                    took = time.monotonic() - started
                    good = 200 <= status < 400
                    ok += good
                    print(f"{'OK  ' if good else 'FAIL'} {base}  HTTP {status} in {took:.1f}s")
                except Exception as exc:
                    took = time.monotonic() - started
                    reason = next((w for w in str(exc).split() if w.startswith("net::")), type(exc).__name__)
                    print(f"FAIL {base}  {reason} after {took:.1f}s")
                finally:
                    await context.close()
        finally:
            await browser.close()
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="also try every known official mirror")
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    base, proxy = sign_in_network(cfg)
    sites = [base] + ([f"https://{h}" for h in BROKER_HOSTS if f"https://{h}" != base] if args.all else [])
    print(f"Sign-in route: {describe_proxy(proxy)}")
    ok = asyncio.run(probe(sites, proxy, cfg.low_memory, cfg.chromium_single_process, cfg.chromium_new_headless))
    if ok == 0:
        print("No login page answered. Set AUTOTRADE_BROWSER_PROXY (see docs/oracle-vps.md), then run this again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

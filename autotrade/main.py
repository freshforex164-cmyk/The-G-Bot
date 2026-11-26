"""Entry point:  python -m autotrade.main   (or: uvicorn autotrade.main:app)"""
from __future__ import annotations

import logging
import os

from .api.app import create_app
from .config import Config
from .logging_utils import install_redaction
from .runtime import build_runtime
from .storage.postgres import PostgresRepository


def build_app():
    logging.basicConfig(level=os.environ.get("AUTOTRADE_LOG_LEVEL", "INFO"))
    install_redaction()
    cfg = Config.from_env()
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL is required")
    repo = PostgresRepository(cfg.database_url)
    rt = build_runtime(cfg, repo=repo)
    return create_app(
        rt,
        before_start=lambda: repo.open(apply_schema=os.environ.get("AUTOTRADE_APPLY_SCHEMA") == "1"),
        after_stop=repo.close,
    )


app = build_app() if os.environ.get("AUTOTRADE_BOOT") == "1" else None

if __name__ == "__main__":
    import uvicorn

    os.environ["AUTOTRADE_BOOT"] = "1"
    # Bind privately on the VPS. A local TLS proxy serves the public login URL.
    uvicorn.run(build_app(), host=os.getenv("AUTOTRADE_HOST", "127.0.0.1"),
                port=int(os.getenv("AUTOTRADE_PORT", "8100")), proxy_headers=True,
                forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"))

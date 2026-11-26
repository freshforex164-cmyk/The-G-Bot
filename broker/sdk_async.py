"""Async initialization bridge for the pinned BinaryOptionsToolsV2 0.2.18 wrapper.

The wrapper's constructor blocks in Rust's runtime.block_on. Its native async
factory avoids that path; mirror only the wrapper's lightweight Python state.
"""
import asyncio
import json
import logging

log = logging.getLogger(__name__)


async def _close_native(client):
    try:
        await asyncio.wait_for(client.shutdown(), timeout=5)
    except Exception:
        log.debug('late broker initialization cleanup failed')


def _close_late(task):
    if task.cancelled():
        return
    try:
        client = task.result()
    except Exception:
        return
    asyncio.create_task(_close_native(client), name='broker-late-initialization-cleanup')


async def open_pocket_option(ssid, *, config=None, timeout=30):
    from BinaryOptionsToolsV2.config import Config
    from BinaryOptionsToolsV2.pocketoption.asynchronous import PocketOptionAsync, _is_guest_auth_payload
    from BinaryOptionsToolsV2.BinaryOptionsToolsV2 import RawPocketOption
    from BinaryOptionsToolsV2.tracing import Logger

    try:
        payload = json.loads(ssid[2:]) if ssid.startswith('42["auth",') else None
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], dict):
        raise ValueError('invalid Pocket Option auth frame')
    guest = _is_guest_auth_payload(payload[1])
    if not guest and not all(key in payload[1] for key in ('session', 'uid')):
        raise ValueError('Pocket Option auth frame requires session and uid')
    cfg = config or Config(terminal_logging=False, connection_initialization_timeout_secs=max(1, int(timeout)))
    api = PocketOptionAsync.__new__(PocketOptionAsync)
    api.logger = Logger()
    api.config = cfg
    api._ssid_format_valid = True
    api._guest_session = guest
    api._init_candles_many_state()
    task = asyncio.ensure_future(RawPocketOption.create_with_config(ssid, cfg.pyconfig))
    try:
        api.client = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except BaseException:
        # A timed-out/cancelled caller must not abandon a native socket that finishes later.
        task.add_done_callback(_close_late)
        raise
    return api

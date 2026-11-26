"""Memory safety for the login browser on a memory-constrained host/container.

* ``container_memory`` reads the cgroup limit/usage so a login is refused (cleanly, with a message) when
  starting Chromium would get the container OOM-killed - which would also stop every running trade worker.
* ``release_memory`` returns freed Python/glibc memory to the OS before Chromium starts and after it closes.
* ``descendant_pids`` / ``reap`` find browser processes started for one login and kill any that survive
  ``close()``, so a hung Chromium can never keep holding memory.
* ``install_quiet_playwright_handler`` stops Playwright futures that fail *after* the browser was closed or
  killed (``TargetClosedError``) from being logged as "Future exception was never retrieved" tracebacks.
"""
from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os
import signal
from typing import Any, Iterable

log = logging.getLogger("autotrade.browser_memory")

MB = 1024 * 1024
_CGROUP_V2 = "/sys/fs/cgroup"
_CGROUP_V1 = "/sys/fs/cgroup/memory"
_UNLIMITED = 1 << 60


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if raw in ("", "max"):
        return _UNLIMITED
    try:
        return int(raw)
    except ValueError:
        return None


def _inactive_file(path: str, key: str) -> int:
    """Page cache the kernel drops before it OOM-kills anything; not real pressure."""
    try:
        with open(path) as fh:
            for line in fh:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


def container_memory(root: str | None = None) -> dict[str, int] | None:
    """{"limit_mb", "used_mb", "free_mb"} for this container, or None when no limit can be read."""
    base = root or _CGROUP_V2
    limit, used = _read_int(f"{base}/memory.max"), _read_int(f"{base}/memory.current")
    cache = _inactive_file(f"{base}/memory.stat", "inactive_file")
    if limit is None or used is None:          # cgroup v1
        v1 = root or _CGROUP_V1
        limit, used = _read_int(f"{v1}/memory.limit_in_bytes"), _read_int(f"{v1}/memory.usage_in_bytes")
        cache = _inactive_file(f"{v1}/memory.stat", "total_inactive_file")
    if limit is None or used is None or limit >= _UNLIMITED or limit <= 0:
        return None
    used = max(0, used - cache)
    return {"limit_mb": limit // MB, "used_mb": used // MB, "free_mb": max(0, limit - used) // MB}


_libc: Any = None


def release_memory() -> None:
    """Collect Python garbage and hand freed heap pages back to the OS (glibc malloc_trim)."""
    global _libc
    gc.collect()
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except Exception:          # not glibc (macOS, musl): garbage collection alone is fine
        _libc = False


# ---- browser processes -----------------------------------------------------------------------------

def _children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                stat = fh.read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def descendant_pids(root: int | None = None) -> set[int]:
    """Every process below ``root`` (default: this one): the Playwright driver and its Chromium."""
    kids, out, stack = _children_map(), set(), [root or os.getpid()]
    while stack:
        for child in kids.get(stack.pop(), []):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X")       # a zombie holds no memory
    except (OSError, IndexError):
        return False


def reap(pids: Iterable[int]) -> list[int]:
    """SIGKILL the given processes if still alive (and collect them if they became our children).
    Returns the pids that had to be killed."""
    killed = []
    for pid in sorted(set(pids)):
        if pid == os.getpid() or not alive(pid):
            _collect(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
        _collect(pid)
    return killed


def _collect(pid: int) -> None:
    # In a container Python can be PID 1, so killed browser processes are re-parented to us; collect only
    # these exact pids (never waitpid(-1), which would steal exit codes from asyncio's own children).
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


# ---- "Future exception was never retrieved" ----------------------------------------------------------

def is_closed_target_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    name = type(exc).__name__
    text = str(exc).lower()
    return name == "TargetClosedError" or (
        "target" in text and "closed" in text) or "browser has been closed" in text or (
        name == "Error" and "connection closed" in text)


def install_quiet_playwright_handler(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Playwright calls still pending when Chromium is closed or killed fail with TargetClosedError on
    futures nobody awaits any more. They are expected; log one short line instead of a traceback.
    Every other unhandled error still goes to the previous handler. Safe to call more than once."""
    loop = loop or asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    if getattr(previous, "_gbot_quiet_playwright", False):
        return

    def handler(lp: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_closed_target_error(context.get("exception")):
            log.info("ignored a browser call that finished after the login browser closed")
            return
        if previous is not None:
            previous(lp, context)
        else:
            lp.default_exception_handler(context)

    handler._gbot_quiet_playwright = True     # type: ignore[attr-defined]
    loop.set_exception_handler(handler)


def consume(fut: "asyncio.Future[Any]") -> None:
    """Done-callback: mark a background Playwright future's exception as retrieved."""
    if not fut.cancelled():
        fut.exception()

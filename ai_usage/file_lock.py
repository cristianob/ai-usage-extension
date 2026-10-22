"""Cross-process file lock usable from asyncio code."""

import asyncio
import fcntl
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


def cache_dir() -> Path:
    """~/.cache/ai-usage, following XDG_CACHE_HOME."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
    return base / "ai-usage"


@asynccontextmanager
async def file_lock(path: Path, timeout: float) -> AsyncIterator[None]:
    """
    Hold an exclusive flock on `path`, raising TimeoutError after `timeout` seconds.

    Polls instead of blocking: coroutines of the same process each hold their own
    descriptor, and a blocking flock would stall the event loop.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"another ai-usage process is holding {path.name}")
                await asyncio.sleep(0.1)
        yield
    finally:
        os.close(fd)

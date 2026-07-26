"""Server-side outbound scheduler: FLOOD_WAIT, per-peer ordering, backoff.

The client has no rate limiting, so every Telegram call funnels through here.
Two lanes keep a burst of avatar downloads from delaying a message send.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

log = logging.getLogger("flood")


class FloodWaitTooLong(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class Scheduler:
    def __init__(self, concurrency: int = 4, low_concurrency: int = 2,
                 max_wait: int = 300, max_retries: int = 2) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._low = asyncio.Semaphore(low_concurrency)
        self._peer_locks: dict[str, asyncio.Lock] = {}
        self.max_wait = max_wait
        self.max_retries = max_retries
        self._paused_until = 0.0

    def _peer_lock(self, peer: str) -> asyncio.Lock:
        lock = self._peer_locks.get(peer)
        if lock is None:
            lock = self._peer_locks[peer] = asyncio.Lock()
        return lock

    async def run(
        self,
        fn: Callable[[], Awaitable[Any]],
        *,
        peer: str | None = None,
        lane: str = "high",
        label: str = "",
    ) -> Any:
        sem = self._sem if lane == "high" else self._low
        lock = self._peer_lock(peer) if peer else None
        async with sem:
            if lock is not None:
                await lock.acquire()
            try:
                return await self._attempt(fn, label)
            finally:
                if lock is not None:
                    lock.release()

    async def _attempt(self, fn: Callable[[], Awaitable[Any]], label: str) -> Any:
        from telethon.errors import FloodWaitError, ServerError, TimedOutError  # local import

        attempt = 0
        while True:
            now = asyncio.get_running_loop().time()
            if self._paused_until > now:
                await asyncio.sleep(self._paused_until - now)
            try:
                return await fn()
            except FloodWaitError as e:
                seconds = int(getattr(e, "seconds", 0) or 0)
                log.warning("FLOOD_WAIT %ss on %s (attempt %s)", seconds, label or "call", attempt)
                if seconds > self.max_wait or attempt >= self.max_retries:
                    raise FloodWaitTooLong(seconds) from None
                self._paused_until = asyncio.get_running_loop().time() + seconds + 1
                await asyncio.sleep(seconds + 1 + random.random())
                attempt += 1
            except (ServerError, TimedOutError) as e:
                if attempt >= self.max_retries:
                    raise
                delay = (2 ** attempt) + random.random()
                log.warning("%s on %s, retrying in %.1fs", type(e).__name__, label, delay)
                await asyncio.sleep(delay)
                attempt += 1


SCHEDULER = Scheduler()

"""Media cache: lazy download, single-flight, LRU eviction, Range serving.

The browser never talks to Telegram. It receives signed URLs on our own origin
and re-fetches whatever it is given.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from . import db
from .config import CFG
from .crypto import cache_key

log = logging.getLogger("media")

_locks: dict[str, asyncio.Lock] = {}
_inflight_paths: set[str] = set()

# Types we refuse to serve inline, because they execute in our origin.
_FORCE_ATTACHMENT = {
    "image/svg+xml", "text/html", "application/xhtml+xml", "text/xml",
    "application/xml", "application/javascript", "text/javascript",
}


def _lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def key_for(kind: str, *parts: object) -> str:
    return cache_key(kind, *parts)


def path_for(key: str) -> Path:
    # Content-addressed and sharded; never derived from user-supplied strings.
    return CFG.media_dir / key[:2] / key[2:4] / key


async def lookup(key: str) -> tuple[Path, str] | None:
    row = await db.fetchone("SELECT rel_path, mime FROM media_cache WHERE key=?", (key,))
    if not row:
        return None
    p = CFG.media_dir / row["rel_path"]
    if not p.exists():
        await db.execute("DELETE FROM media_cache WHERE key=?", (key,))
        return None
    await db.execute("UPDATE media_cache SET last_access=? WHERE key=?", (db.now_ms(), key))
    return p, (row["mime"] or "application/octet-stream")


async def store(key: str, tmp: Path, mime: str) -> tuple[Path, str]:
    dest = path_for(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    try:
        dest.chmod(0o600)
    except PermissionError:
        pass
    size = dest.stat().st_size
    now = db.now_ms()
    await db.execute(
        "INSERT INTO media_cache(key, rel_path, bytes, mime, created_at, last_access)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " rel_path=excluded.rel_path, bytes=excluded.bytes, mime=excluded.mime,"
        " last_access=excluded.last_access",
        (key, str(dest.relative_to(CFG.media_dir)), size, mime, now, now),
    )
    return dest, mime


async def get_or_fetch(key: str, producer) -> tuple[Path, str]:
    """producer() -> (path|bytes, mime). Called at most once per key at a time."""
    hit = await lookup(key)
    if hit:
        return hit
    async with _lock(key):
        hit = await lookup(key)
        if hit:
            return hit
        blob, mime = await producer()
        tmp = CFG.media_dir / f".tmp-{key}-{os.getpid()}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(blob, (bytes, bytearray)):
            tmp.write_bytes(blob)
        else:
            src = Path(blob)
            if src != tmp:
                os.replace(src, tmp)
        return await store(key, tmp, mime)


def safe_mime(mime: str) -> tuple[str, bool]:
    """Returns (content_type, force_attachment)."""
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if mime in _FORCE_ATTACHMENT:
        return "application/octet-stream", True
    return mime, False


async def sweep() -> int:
    """LRU eviction down to the byte budget. Never touches an in-flight file."""
    row = await db.fetchone("SELECT COALESCE(SUM(bytes),0) total FROM media_cache")
    total = row["total"] or 0
    if total <= CFG.media_cache_max_bytes:
        return 0
    freed = 0
    rows = await db.fetchall("SELECT key, rel_path, bytes FROM media_cache ORDER BY last_access ASC")
    for r in rows:
        if total - freed <= CFG.media_cache_max_bytes * 0.9:
            break
        if r["rel_path"] in _inflight_paths:
            continue
        p = CFG.media_dir / r["rel_path"]
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not evict %s: %s", r["key"], e)
            continue
        await db.execute("DELETE FROM media_cache WHERE key=?", (r["key"],))
        freed += r["bytes"]
    log.info("evicted %.1f MiB from the media cache", freed / 1048576)
    return freed


async def sweeper_task(interval: int = 600) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            await sweep()
            cutoff = db.now_ms() - 3600_000
            stale = await db.fetchall(
                "SELECT handle, rel_path FROM upload_handles WHERE consumed=0 AND created_at < ?",
                (cutoff,),
            )
            for r in stale:
                if r["rel_path"]:
                    (CFG.upload_dir / r["rel_path"]).unlink(missing_ok=True)
                await db.execute("DELETE FROM upload_handles WHERE handle=?", (r["handle"],))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("media sweeper hiccup")


def mark_inflight(rel_path: str) -> None:
    _inflight_paths.add(rel_path)


def clear_inflight(rel_path: str) -> None:
    _inflight_paths.discard(rel_path)


def decode_waveform(packed: bytes) -> list[int]:
    """Telegram packs voice waveforms as 5-bit values (0-31).

    The contract wants ``wave: [0-32]`` peaks, so this is a direct unpack - no
    audio decoding, no ffmpeg.
    """
    if not packed:
        return []
    bits = int.from_bytes(packed, "little")
    count = (len(packed) * 8) // 5
    out = [(bits >> (5 * i)) & 0x1F for i in range(count)]
    # The UI draws ~46 bars; downsample evenly so long notes stay readable.
    target = 48
    if len(out) > target:
        step = len(out) / target
        out = [max(out[int(i * step): int((i + 1) * step)] or [0]) for i in range(target)]
    return out


def human_size(n: int) -> str:
    """The frontend prints fileSize verbatim, so format it here."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: int | float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def now_s() -> int:
    return int(time.time())

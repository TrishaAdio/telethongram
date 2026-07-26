"""SQLite (WAL) persistence. One file, no daemon, trivial to back up."""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional

import aiosqlite

from .config import CFG

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS enabled_chats (
  client_id   TEXT PRIMARY KEY,
  tg_chat_id  INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  added_at    INTEGER NOT NULL,
  added_via   TEXT NOT NULL DEFAULT 'addweb',
  hide_before INTEGER NOT NULL DEFAULT 0   -- local clearHistory watermark (ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_enabled_tg ON enabled_chats(tg_chat_id);

CREATE TABLE IF NOT EXISTS entities (
  client_id TEXT PRIMARY KEY,
  tg_id     INTEGER NOT NULL,
  kind      TEXT NOT NULL,
  title     TEXT,
  username  TEXT,
  photo_id  TEXT,
  seen_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_tg ON entities(tg_id);

CREATE TABLE IF NOT EXISTS msg_ids (
  client_id  TEXT PRIMARY KEY,
  chat_id    TEXT NOT NULL,
  tg_msg_id  INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS filtered_msgs (
  tg_chat_id INTEGER NOT NULL,
  tg_msg_id  INTEGER NOT NULL,
  reason     TEXT NOT NULL,
  at         INTEGER NOT NULL,
  PRIMARY KEY (tg_chat_id, tg_msg_id)
);

CREATE TABLE IF NOT EXISTS drafts (
  chat_id    TEXT PRIMARY KEY,
  text       TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_cache (
  key         TEXT PRIMARY KEY,
  rel_path    TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  mime        TEXT,
  created_at  INTEGER NOT NULL,
  last_access INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_lru ON media_cache(last_access);

CREATE TABLE IF NOT EXISTS upload_handles (
  handle     TEXT PRIMARY KEY,
  rel_path   TEXT,
  name       TEXT NOT NULL,
  size       INTEGER NOT NULL,
  mime       TEXT,
  synthetic  INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  consumed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_sessions (
  id         TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  csrf       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  ip         TEXT,
  ua_hash    TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ip      TEXT NOT NULL,
  at      INTEGER NOT NULL,
  outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_ip ON login_attempts(ip, at);

CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     INTEGER NOT NULL,
  event  TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id  TEXT NOT NULL,
  chat_id    TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  state      TEXT NOT NULL DEFAULT 'queued'
);
"""

_conn: Optional[aiosqlite.Connection] = None


def now_ms() -> int:
    return int(time.time() * 1000)


async def connect() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        CFG.ensure_dirs()
        _conn = await aiosqlite.connect(CFG.db_path)
        _conn.row_factory = aiosqlite.Row
        await _conn.executescript(SCHEMA)
        await _conn.commit()
        try:
            CFG.db_path.chmod(0o600)
        except PermissionError:
            pass
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    c = await connect()
    await c.execute(sql, tuple(params))
    await c.commit()


async def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
    c = await connect()
    async with c.execute(sql, tuple(params)) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    c = await connect()
    async with c.execute(sql, tuple(params)) as cur:
        return list(await cur.fetchall())


async def audit(event: str, detail: str = "") -> None:
    await execute("INSERT INTO audit(at, event, detail) VALUES(?,?,?)", (now_ms(), event, detail))

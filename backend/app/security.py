"""Web-app authentication, CSRF, origin checks and rate limiting.

This service holds a session that controls a real Telegram account, so the
front door is treated as the most important surface in the codebase.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Request

from . import db
from .config import CFG
from .crypto import new_token, token_hash

log = logging.getLogger("security")

# argon2id, deliberately expensive: one login per week, no throughput concern.
PH = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

LOGIN_MAX_PER_IP = 5           # per window
LOGIN_WINDOW = 15 * 60         # seconds
LOGIN_MAX_GLOBAL = 20          # per hour, all IPs
GENERIC_LOGIN_ERROR = "That passphrase was not accepted."


def hash_password(passphrase: str) -> str:
    return PH.hash(passphrase)


def verify_password(passphrase: str) -> bool:
    if not CFG.password_hash:
        return False
    try:
        return PH.verify(CFG.password_hash, passphrase)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def client_ip(request: Request) -> str:
    # Only trust the proxy header when we are actually behind our own proxy.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd and request.client and request.client.host in ("127.0.0.1", "::1"):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def ua_hash(request: Request) -> str:
    return hashlib.sha256(request.headers.get("user-agent", "").encode()).hexdigest()[:32]


# --- login throttle --------------------------------------------------------

async def login_blocked(ip: str) -> int:
    """Returns seconds to wait, or 0 when a login attempt is allowed."""
    now = int(time.time())
    row = await db.fetchone(
        "SELECT COUNT(*) n, MAX(at) last FROM login_attempts "
        "WHERE ip=? AND outcome='fail' AND at > ?",
        (ip, (now - LOGIN_WINDOW) * 1000),
    )
    n, last = (row["n"] or 0), (row["last"] or 0) // 1000
    if n >= LOGIN_MAX_PER_IP:
        # exponential backoff on top of the window, capped at the window itself
        wait = min(LOGIN_WINDOW, 2 ** min(n - LOGIN_MAX_PER_IP, 8) * 5)
        remaining = (last + wait) - now
        if remaining > 0:
            return remaining
    grow = await db.fetchone(
        "SELECT COUNT(*) n FROM login_attempts WHERE outcome='fail' AND at > ?",
        ((now - 3600) * 1000,),
    )
    if (grow["n"] or 0) >= LOGIN_MAX_GLOBAL:
        return 300
    return 0


async def record_attempt(ip: str, outcome: str) -> None:
    await db.execute(
        "INSERT INTO login_attempts(ip, at, outcome) VALUES(?,?,?)", (ip, db.now_ms(), outcome)
    )
    await db.execute("DELETE FROM login_attempts WHERE at < ?", (db.now_ms() - 7 * 86400_000,))


# --- sessions --------------------------------------------------------------

@dataclass
class Session:
    id: str
    csrf: str


async def create_session(request: Request) -> tuple[str, Session]:
    token = new_token()
    sid = new_token()[:22]
    csrf = new_token()
    now = db.now_ms()
    await db.execute(
        "INSERT INTO web_sessions(id, token_hash, csrf, created_at, last_seen, expires_at, ip, ua_hash)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            sid,
            token_hash(token),
            csrf,
            now,
            now,
            now + CFG.session_ttl_days * 86400_000,
            client_ip(request),
            ua_hash(request),
        ),
    )
    await db.audit("login", f"ip={client_ip(request)} sid={sid}")
    return token, Session(id=sid, csrf=csrf)


async def load_session(request: Request) -> Session | None:
    token = request.cookies.get(CFG.cookie_name)
    if not token:
        return None
    row = await db.fetchone(
        "SELECT id, csrf, expires_at, ua_hash FROM web_sessions WHERE token_hash=?",
        (token_hash(token),),
    )
    if not row:
        return None
    if row["expires_at"] < db.now_ms():
        await db.execute("DELETE FROM web_sessions WHERE id=?", (row["id"],))
        return None
    if row["ua_hash"] and row["ua_hash"] != ua_hash(request):
        log.warning("session %s presented from a different user agent; rejecting", row["id"])
        return None
    await db.execute(
        "UPDATE web_sessions SET last_seen=?, expires_at=? WHERE id=?",
        (db.now_ms(), db.now_ms() + CFG.session_ttl_days * 86400_000, row["id"]),
    )
    return Session(id=row["id"], csrf=row["csrf"])


async def destroy_session(request: Request) -> None:
    token = request.cookies.get(CFG.cookie_name)
    if token:
        row = await db.fetchone("SELECT id FROM web_sessions WHERE token_hash=?", (token_hash(token),))
        if row:
            await db.audit("logout", f"sid={row['id']}")
        await db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash(token),))


def origin_ok(request: Request) -> bool:
    """Mandatory for WS (SameSite does not protect it) and cheap for POSTs."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return True  # same-origin GET/img requests often omit it
    if CFG.allowed_origins:
        return origin in CFG.allowed_origins
    host = request.headers.get("host", "")
    return origin.split("//")[-1] == host


# --- endpoint rate limiting ------------------------------------------------

class TokenBucket:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self.tokens, self.ts = burst, time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


CLASS_LIMITS = {
    "read": (30.0, 60.0),
    "write": (5.0, 15.0),
    "search": (2.0, 6.0),
    "media": (50.0, 100.0),
    "upload": (1.0, 3.0),
    "login": (0.5, 3.0),
}

_buckets: dict[tuple[str, str], TokenBucket] = {}


def allow(scope: str, klass: str) -> bool:
    rate, burst = CLASS_LIMITS.get(klass, (10.0, 20.0))
    key = (scope, klass)
    b = _buckets.get(key)
    if b is None:
        b = _buckets[key] = TokenBucket(rate, burst)
    return b.take()


RATE_LIMIT_MESSAGE = "Slow down a moment — too many requests at once. Try again in a second."

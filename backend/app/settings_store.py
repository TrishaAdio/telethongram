"""Server-owned settings. No localStorage on the client, per BACKEND.md.

Presentation keys live here. Privacy keys (lastSeen / profilePhoto / calls) and
twoStep are owned by Telegram; the Telethon gateway overlays the real values on
read and pushes changes on write.
"""
from __future__ import annotations

import json
from typing import Any

from . import db

DEFAULTS: dict[str, Any] = {
    "theme": "light",
    "bubble": "cyan",
    "wallpaper": "paper",
    "fontSize": 16,
    "lastSeen": "everybody",
    "profilePhoto": "contacts",
    "calls": "contacts",
    "twoStep": False,
    "notifications": {"global": True, "sound": True, "preview": True},
    "language": "English (US)",
}

PRIVACY_KEYS = ("lastSeen", "profilePhoto", "calls")
UI_KEYS = ("theme", "bubble", "wallpaper", "fontSize", "notifications", "language")

_ENUMS = {
    "theme": {"light", "dark", "system"},
    "bubble": {"cyan", "magenta", "ink"},
    "wallpaper": {"paper", "grid", "tint"},
    "lastSeen": {"everybody", "contacts", "nobody"},
    "profilePhoto": {"everybody", "contacts", "nobody"},
    "calls": {"everybody", "contacts", "nobody"},
}


def sanitize(payload: dict) -> dict:
    """Drop anything the contract does not define; clamp what it bounds."""
    out: dict[str, Any] = {}
    for k, v in (payload or {}).items():
        if k not in DEFAULTS:
            continue
        if k in _ENUMS:
            if v in _ENUMS[k]:
                out[k] = v
        elif k == "fontSize":
            try:
                out[k] = max(13, min(20, int(v)))
            except (TypeError, ValueError):
                pass
        elif k == "notifications" and isinstance(v, dict):
            out[k] = {n: bool(v.get(n, DEFAULTS[k][n])) for n in ("global", "sound", "preview")}
        elif k == "language" and isinstance(v, str):
            out[k] = v[:40]
        elif k == "twoStep":
            # Enabling 2FA needs a password the UI cannot collect, so the value
            # is reported from Telegram and never written from here.
            continue
    return out


async def load() -> dict:
    rows = await db.fetchall("SELECT k, v FROM settings")
    stored = {r["k"]: json.loads(r["v"]) for r in rows}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return merged


async def save(patch: dict) -> dict:
    for k, v in patch.items():
        await db.execute(
            "INSERT INTO settings(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )
    return await load()

"""Client id <-> Telegram int64 mapping.

Three ids are hard-coded in the frontend and cannot be chosen freely:
  * ``u_me``    - self (reaction highlighting, "(you)" label)
  * ``c_saved`` - Saved Messages (the Forward action targets it literally)
  * ``c_city``  - the desktop boot chat; aliased in api.js to the top chat
int64 values are only ever handed to the browser as strings.
"""
from __future__ import annotations

ME = "u_me"
SAVED = "c_saved"

INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def parse_int64(value: object) -> int:
    """Strict int64 parse. Never goes through float, so precision is exact."""
    if isinstance(value, bool):
        raise ValueError("not an id")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        s = value.strip()
        if not s or (s[0] not in "-0123456789") or not s.lstrip("-").isdigit():
            raise ValueError(f"not an integer id: {value!r}")
        n = int(s)
    else:
        raise ValueError(f"not an integer id: {type(value).__name__}")
    if not (INT64_MIN <= n <= INT64_MAX):
        raise ValueError("id out of int64 range")
    return n


def s64(value: object) -> str | None:
    """Serialise an int64 for the wire. Always a string, never a JS number."""
    if value is None:
        return None
    return str(parse_int64(value))


def user_cid(tg_user_id: int, me_id: int | None = None) -> str:
    if me_id is not None and int(tg_user_id) == int(me_id):
        return ME
    return f"u{parse_int64(tg_user_id)}"


def chat_cid(tg_chat_id: int, kind: str, me_id: int | None = None) -> str:
    tg = parse_int64(tg_chat_id)
    if kind == "saved" or (me_id is not None and tg == int(me_id)):
        return SAVED
    if kind == "private":
        return f"c_u{tg}"
    if kind == "channel":
        return f"c_ch{tg}"
    return f"c_g{tg}"


def msg_cid(tg_msg_id: int) -> str:
    return f"m{parse_int64(tg_msg_id)}"


def is_client_msg_id(mid: str) -> bool:
    return isinstance(mid, str) and mid.startswith("mo_")


def sender_color(tg_user_id: int) -> int:
    """Stable palette index 0-7. Never a hex - the client owns the colours."""
    h = 0
    for ch in str(tg_user_id):
        h = (h * 31 + ord(ch)) % 997
    return h % 8


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("_", " ").split() if p]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

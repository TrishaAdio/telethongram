"""Logging with a redaction filter.

Nothing here is allowed to emit: the session string, API hash, cookie or session
tokens, the media signing key, passphrases, or message bodies. The filter is a
backstop for accidents, not a licence to log secrets.
"""
from __future__ import annotations

import logging
import os
import re

from .config import CFG

_SENSITIVE_KEYS = re.compile(
    r"(?i)(session_string|session|api_hash|password|passphrase|secret|token|cookie|sig)"
    r"\s*[=:]\s*['\"]?([^\s'\",;]{6,})"
)
# Telethon StringSessions are long base64-ish blobs; never let one through.
_LONG_BLOB = re.compile(r"\b[A-Za-z0-9_\-+/=]{120,}\b")


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = _SENSITIVE_KEYS.sub(lambda m: f"{m.group(1)}=<redacted>", msg)
        red = _LONG_BLOB.sub("<redacted>", red)
        if CFG.api_hash:
            red = red.replace(CFG.api_hash, "<redacted>")
        if CFG.secret_key:
            red = red.replace(CFG.secret_key, "<redacted>")
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Telethon is chatty and logs request objects; keep it above DEBUG.
    logging.getLogger("telethon").setLevel(max(logging.INFO, root.level))

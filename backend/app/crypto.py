"""Encryption at rest for the session string, and HMAC signing for media URLs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import CFG

_MAGIC = b"TGG1"


def _subkey(info: bytes, length: int = 32) -> bytes:
    """HKDF-Expand-ish derivation from SECRET_KEY. One KEK, several purposes."""
    if not CFG.secret_key:
        raise RuntimeError("SECRET_KEY is not configured")
    prk = hmac.new(b"telethongram", CFG.secret_key.encode(), hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


# --- session string at rest ------------------------------------------------

def seal(plaintext: str) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(_subkey(b"session")).encrypt(nonce, plaintext.encode(), _MAGIC)
    return _MAGIC + nonce + ct


def unseal(blob: bytes) -> str:
    if not blob.startswith(_MAGIC):
        raise ValueError("session file is not in the expected format")
    nonce, ct = blob[4:16], blob[16:]
    return AESGCM(_subkey(b"session")).decrypt(nonce, ct, _MAGIC).decode()


# --- media URL signing -----------------------------------------------------

def sign_media(path: str, exp: int, session_id: str) -> str:
    msg = f"{path}\n{exp}\n{session_id}".encode()
    mac = hmac.new(_subkey(b"media-url"), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def verify_media(path: str, exp: int, session_id: str, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sign_media(path, exp, session_id), sig or "")


def signed_media_url(path: str, session_id: str) -> str:
    exp = int(time.time()) + CFG.media_url_ttl
    return f"{path}?exp={exp}&sig={sign_media(path, exp, session_id)}"


# --- tokens ----------------------------------------------------------------

def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cache_key(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()

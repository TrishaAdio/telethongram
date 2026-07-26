"""Configuration and secret loading.

Secrets are never committed and never returned by any endpoint. The only value
that must live in the environment is SECRET_KEY (the key-encryption key); the
Telegram session string itself is stored encrypted on disk at 0600 and is
decrypted in memory by the MTProto worker only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


@dataclass
class Config:
    # --- process -----------------------------------------------------------
    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = _int("PORT", 4000)
    gateway: str = os.environ.get("GATEWAY", "fake").strip().lower()  # fake | telethon

    # --- paths -------------------------------------------------------------
    data_dir: Path = Path(os.environ.get("DATA_DIR", "/var/lib/telethongram"))
    frontend_dir: Path = Path(
        os.environ.get("FRONTEND_DIR", str(Path(__file__).resolve().parents[2] / "frontend"))
    )

    # --- web auth ----------------------------------------------------------
    # argon2id hash of the login passphrase, produced by `python -m backend.cli hash`
    password_hash: str = os.environ.get("WEB_PASSWORD_HASH", "")
    secret_key: str = field(default="", repr=False)
    session_ttl_days: int = _int("SESSION_TTL_DAYS", 7)
    cookie_secure: bool = _bool("COOKIE_SECURE", True)
    cookie_name: str = "tg_sid"
    allowed_origins: tuple = ()

    # --- telegram ----------------------------------------------------------
    api_id: int = _int("TG_API_ID", 0)
    api_hash: str = field(default_factory=lambda: os.environ.get("TG_API_HASH", ""), repr=False)

    # --- behaviour ---------------------------------------------------------
    command_prefix: str = os.environ.get("COMMAND_PREFIX", ".")
    delete_command_messages: bool = _bool("DELETE_COMMAND_MESSAGES", False)
    mirror_dialog_state: bool = _bool("MIRROR_DIALOG_STATE", True)   # pin/mute/archive hit Telegram
    telegram_drafts: bool = _bool("TELEGRAM_DRAFTS", False)          # drafts local by default
    destructive_clear_history: bool = _bool("DESTRUCTIVE_CLEAR_HISTORY", False)
    auto_enable_saved: bool = _bool("AUTO_ENABLE_SAVED", True)

    # --- media -------------------------------------------------------------
    media_url_ttl: int = _int("MEDIA_URL_TTL", 900)
    media_cache_max_bytes: int = _int("MEDIA_CACHE_MAX_BYTES", 5 * 1024 ** 3)
    media_max_download_bytes: int = _int("MEDIA_MAX_DOWNLOAD_BYTES", 100 * 1024 ** 2)
    upload_max_bytes: int = _int("UPLOAD_MAX_BYTES", 100 * 1024 ** 2)

    def __post_init__(self) -> None:
        key = os.environ.get("SECRET_KEY", "").strip()
        if not key:
            key_file = os.environ.get("SECRET_KEY_FILE", "").strip()
            if key_file and Path(key_file).exists():
                key = Path(key_file).read_text().strip()
        object.__setattr__(self, "secret_key", key)
        origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
        object.__setattr__(
            self,
            "allowed_origins",
            tuple(o.strip().rstrip("/") for o in origins.split(",") if o.strip()),
        )

    # --- derived paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.enc"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.media_dir, self.upload_dir):
            p.mkdir(parents=True, exist_ok=True)
            try:
                p.chmod(0o700)
            except PermissionError:
                pass

    def blockers(self) -> list[str]:
        """Things that make the bridge unable to work at all."""
        out = []
        # systemd's EnvironmentFile keeps everything after the "=", including a
        # trailing "# comment". Catch that before it silently changes behaviour.
        for name in ("GATEWAY", "TG_API_HASH", "HOST", "DATA_DIR", "FRONTEND_DIR",
                     "ALLOWED_ORIGINS", "COMMAND_PREFIX"):
            raw = os.environ.get(name, "")
            if "#" in raw:
                out.append(
                    f"{name} contains a '#' — an inline comment was read as part of the "
                    f"value. Put comments on their own line in the env file."
                )
        if self.gateway not in ("fake", "telethon"):
            out.append(f"GATEWAY is '{self.gateway}'; expected 'telethon' or 'fake'.")
        if not self.secret_key or len(self.secret_key) < 32:
            out.append("SECRET_KEY is missing or shorter than 32 characters.")
        if not self.password_hash.startswith("$argon2"):
            if self.password_hash:
                out.append(
                    "WEB_PASSWORD_HASH does not look like an argon2 hash. If you sourced the "
                    "env file in a shell, bash expanded the '$' segments away — wrap the value "
                    "in single quotes in /etc/telethongram/env."
                )
            else:
                out.append("WEB_PASSWORD_HASH is missing (run: python -m backend.cli hash).")
        if self.gateway == "telethon" and not (self.api_id and self.api_hash):
            out.append("TG_API_ID / TG_API_HASH are required when GATEWAY=telethon.")
        if self.gateway == "telethon" and not self.session_path.exists():
            out.append(
                f"No Telegram session at {self.session_path} (run: python -m backend.cli login)."
            )
        return out

    def advisories(self) -> list[str]:
        """Things worth saying out loud that must never stop the bridge."""
        out = []
        if not self.cookie_secure and self.host not in ("127.0.0.1", "::1", "localhost"):
            out.append(
                "COOKIE_SECURE=false while listening on a public interface: the login cookie "
                "and every message will cross the network unencrypted. Put TLS in front, or "
                "tunnel over SSH and keep HOST=127.0.0.1."
            )
        if any("example.com" in o for o in self.allowed_origins):
            out.append(
                "ALLOWED_ORIGINS still points at example.com — every request from your real "
                "domain will be rejected. Set it to your own URL, or leave it empty to accept "
                "whatever Host the request arrives with."
            )
        return out

    def problems(self) -> list[str]:
        """Everything, for the CLI self-check."""
        return self.blockers() + self.advisories()


CFG = Config()

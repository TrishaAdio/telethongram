"""One-time admin tasks, run over SSH on the VPS.

Telegram sign-in deliberately lives here and not on the web tier: no
code-request endpoint is ever exposed to the internet, and the session string is
written straight to disk encrypted.

    python -m backend.cli keygen     # SECRET_KEY for the env file
    python -m backend.cli hash       # WEB_PASSWORD_HASH for the env file
    python -m backend.cli login      # sign in to Telegram, encrypt the session
    python -m backend.cli check      # config self-check
    python -m backend.cli logout-all # end every web session (not Telegram)
"""
from __future__ import annotations

import asyncio
import getpass
import secrets
import sys

from .app.config import CFG
from .app.crypto import seal
from .app.security import hash_password


def cmd_keygen() -> int:
    print(secrets.token_urlsafe(48))
    return 0


def cmd_hash() -> int:
    first = getpass.getpass("New web passphrase: ")
    if len(first) < 12:
        print("Use at least 12 characters — this guards a full Telegram account.", file=sys.stderr)
        return 1
    if first != getpass.getpass("Repeat: "):
        print("Those did not match.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


async def _login() -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not (CFG.api_id and CFG.api_hash):
        print("Set TG_API_ID and TG_API_HASH first (my.telegram.org).", file=sys.stderr)
        return 1
    if not CFG.secret_key:
        print("Set SECRET_KEY first (python -m backend.cli keygen).", file=sys.stderr)
        return 1

    client = TelegramClient(StringSession(), CFG.api_id, CFG.api_hash)
    await client.connect()
    phone = input("Phone number (with country code): ").strip()
    await client.send_code_request(phone)
    code = input("Code Telegram just sent: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except Exception as e:
        if "password" in str(e).lower() or type(e).__name__ == "SessionPasswordNeededError":
            await client.sign_in(password=getpass.getpass("Two-step password: "))
        else:
            print(f"Sign-in failed: {type(e).__name__}", file=sys.stderr)
            await client.disconnect()
            return 1

    me = await client.get_me()
    session_string = client.session.save()
    CFG.ensure_dirs()
    CFG.session_path.write_bytes(seal(session_string))
    CFG.session_path.chmod(0o600)
    del session_string
    await client.disconnect()
    print(f"Signed in as {me.first_name} (id {me.id}).")
    print(f"Encrypted session written to {CFG.session_path} (0600).")
    print("It is only readable with this SECRET_KEY. Back up both, or neither.")
    return 0


def cmd_check() -> int:
    blockers, advisories = CFG.blockers(), CFG.advisories()
    print(f"gateway={CFG.gateway} port={CFG.port} data_dir={CFG.data_dir}")
    print(f"frontend={CFG.frontend_dir}")
    print(f"cookie_secure={CFG.cookie_secure} mirror_dialog_state={CFG.mirror_dialog_state}")
    print(f"delete_command_messages={CFG.delete_command_messages} "
          f"destructive_clear_history={CFG.destructive_clear_history}")
    for p in advisories:
        print(f"warning: {p}")
    if not blockers:
        print("config: OK" + (" (with warnings)" if advisories else ""))
        return 0
    for p in blockers:
        print(f"blocked: {p}")
    return 1


async def _logout_all() -> int:
    from .app import db
    await db.connect()
    await db.execute("DELETE FROM web_sessions", ())
    await db.audit("logout_all", "cli")
    await db.close()
    print("Every Telethongram web session has been ended. Telegram is untouched.")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "keygen":
        return cmd_keygen()
    if cmd == "hash":
        return cmd_hash()
    if cmd == "login":
        return asyncio.run(_login())
    if cmd == "check":
        return cmd_check()
    if cmd == "logout-all":
        return asyncio.run(_logout_all())
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

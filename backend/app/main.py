"""HTTP + WebSocket bridge.

The contract in BACKEND.md is function-shaped (46 named functions), so the API
surface mirrors it 1:1 as a single authenticated RPC endpoint instead of 46
bespoke routes. That keeps the envelope, CSRF handling and rate-limit classes in
exactly one place, and keeps api.js small.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)

from . import db, ids, logging_setup, media, security
from .config import CFG
from .crypto import signed_media_url, verify_media
from .gateway.base import Gateway, UserFacing
from .hub import HUB, HANDLER_NAMES

logging_setup.setup()
log = logging.getLogger("bridge")

app = FastAPI(title="Telethongram bridge", docs_url=None, redoc_url=None, openapi_url=None)
GW: Gateway | None = None

GENERIC_ERROR = "Something went wrong on the Telegram bridge. Try again in a moment."
NO_SESSION_ERROR = "Your Telethongram session expired. Reload the page to sign in again."

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not no-referrer: that makes browsers send `Origin: null` on form posts,
    # which the CSRF check cannot distinguish from a genuine cross-site request.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    # This policy is as tight as the frontend allows, and no tighter.
    # support.js loads React, ReactDOM and Babel standalone from unpkg at runtime
    # and compiles the page's <script data-dc-script> with new Function, so
    # 'unsafe-eval' and that origin are both required. Removing either one blanks
    # the page. See the CSP note in the README for what that costs.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


# --------------------------------------------------------------------- boot

@app.on_event("startup")
async def _startup() -> None:
    global GW
    await db.connect()
    CFG.ensure_dirs()
    if CFG.gateway == "telethon":
        from .gateway.telethon_gw import TelethonGateway
        GW = TelethonGateway()
    else:
        from .gateway.fake import FakeGateway
        GW = FakeGateway()
        log.warning("running with the FAKE gateway - no real Telegram account is connected")
    try:
        await GW.start()
    except Exception as e:
        log.error("gateway failed to start: %s", e)
        HUB.set_status("offline")
    app.state.sweeper = asyncio.create_task(media.sweeper_task())
    for p in CFG.blockers():
        log.error("config: %s", p)
    for p in CFG.advisories():
        log.warning("config: %s", p)


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "sweeper", None)
    if task:
        task.cancel()
    if GW:
        await GW.stop()
    await db.close()


@app.middleware("http")
async def _headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if CFG.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# ------------------------------------------------------------------- login

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Telethongram</title>
<style>
:root{color-scheme:light dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f2f2;color:#201e1d;
font:16px/1.5 "Public Sans",system-ui,sans-serif}
form{width:min(360px,92vw);background:#fff;border:1px solid rgba(0,0,0,.14);padding:28px}
h1{margin:0 0 4px;font-size:20px;letter-spacing:-.01em}
p{margin:0 0 20px;color:#6b6766;font-size:14px}
label{display:block;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#6b6766;margin-bottom:6px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid rgba(0,0,0,.22);border-radius:2px;font:inherit}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:2px;background:#0f6b86;color:#fff;
font:inherit;font-weight:600;cursor:pointer}
.err{margin:14px 0 0;padding:9px 11px;background:#fdecec;border-left:3px solid #a10b52;color:#7a1030;font-size:14px}
code{font-family:"IBM Plex Mono",ui-monospace,monospace}
</style></head><body>
<form method="post" action="/login" autocomplete="off">
<h1>Telethongram</h1>
<p>This bridge signs in as your own Telegram account. Enter the passphrase.</p>
<label for="p">Passphrase</label>
<input id="p" name="passphrase" type="password" autofocus required>
<button type="submit">Sign in</button>
__ERROR__
</form></body></html>"""


def _login_html(error: str = "") -> str:
    block = f'<p class="err">{error}</p>' if error else ""
    return LOGIN_PAGE.replace("__ERROR__", block)


@app.get("/login")
async def login_page(request: Request):
    if await security.load_session(request):
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_login_html())


@app.post("/login")
async def login_submit(request: Request, passphrase: str = Form("")):
    ip = security.client_ip(request)
    if not security.origin_ok(request):
        return HTMLResponse(_login_html("That request did not come from this site."), status_code=403)
    if not security.allow(ip, "login"):
        # Recorded too, so the audit trail shows the whole attempt sequence and
        # not just the ones that got as far as a password check.
        await security.record_attempt(ip, "throttled")
        return HTMLResponse(_login_html(security.RATE_LIMIT_MESSAGE), status_code=429)
    wait = await security.login_blocked(ip)
    if wait:
        await security.record_attempt(ip, "blocked")
        mins = max(1, round(wait / 60))
        return HTMLResponse(
            _login_html(f"Too many attempts. Try again in about {mins} minute{'s' if mins != 1 else ''}."),
            status_code=429,
        )
    if not security.verify_password(passphrase):
        await security.record_attempt(ip, "fail")
        log.warning("failed login from %s", ip)
        return HTMLResponse(_login_html(security.GENERIC_LOGIN_ERROR), status_code=401)

    await security.record_attempt(ip, "ok")
    token, session = await security.create_session(request)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        CFG.cookie_name, token, httponly=True, secure=CFG.cookie_secure, samesite="lax",
        max_age=CFG.session_ttl_days * 86400, path="/",
    )
    response.set_cookie(
        "tg_csrf", session.csrf, httponly=False, secure=CFG.cookie_secure, samesite="lax",
        max_age=CFG.session_ttl_days * 86400, path="/",
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    await security.destroy_session(request)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(CFG.cookie_name, path="/")
    response.delete_cookie("tg_csrf", path="/")
    return response


# ------------------------------------------------------------------- static

def _frontend(path: str) -> Path:
    return CFG.frontend_dir / path


@app.get("/")
async def index(request: Request):
    if not await security.load_session(request):
        return RedirectResponse("/login", status_code=302)
    page = _frontend("Messenger.dc.html")
    if not page.exists():
        return PlainTextResponse("frontend/Messenger.dc.html is missing", status_code=500)
    return HTMLResponse(page.read_text(), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "gateway": GW.name if GW else "none",
        "connection": HUB.status,
        # Enough to diagnose a sign-in that is being refused, with no secrets.
        "cookieSecure": CFG.cookie_secure,
        "allowedOrigins": list(CFG.allowed_origins),
        "listen": f"{CFG.host}:{CFG.port}",
    }


# ---------------------------------------------------------------------- rpc

def _sign_urls(value: Any, session_id: str) -> Any:
    """Walk a response and sign every media path we hand to the browser."""
    if isinstance(value, str):
        if value.startswith("/media/") and "sig=" not in value:
            return signed_media_url(value, session_id)
        return value
    if isinstance(value, list):
        return [_sign_urls(v, session_id) for v in value]
    if isinstance(value, dict):
        return {k: _sign_urls(v, session_id) for k, v in value.items()}
    return value


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data})


def _err(message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _method_table() -> dict[str, tuple[Callable, str]]:
    g = GW
    return {
        # reads
        "getChats":       (lambda a: g.get_chats(), "read"),
        "getChat":        (lambda a: g.get_chat(a[0]), "read"),
        "getMessages":    (lambda a: g.get_messages(a[0], (a[1] or {}).get("before"),
                                                    int((a[1] or {}).get("limit") or 40)), "read"),
        "getProfile":     (lambda a: g.get_profile(a[0]), "read"),
        "getSharedMedia": (lambda a: g.get_shared_media(a[0], a[1] if len(a) > 1 else None), "read"),
        "searchAll":      (lambda a: g.search_all(a[0]), "search"),
        "searchInChat":   (lambda a: g.search_in_chat(a[0], a[1]), "search"),
        "getContacts":    (lambda a: g.get_contacts(), "read"),
        "getSettings":    (lambda a: g.get_settings(), "read"),
        "getBlocked":     (lambda a: g.get_blocked(), "read"),
        "getSessions":    (lambda a: g.get_sessions(), "read"),
        # writes
        "sendMessage":    (lambda a: g.send_message(a[0], a[1] or {}), "write"),
        "editMessage":    (lambda a: g.edit_message(a[0], a[1], a[2]), "write"),
        "deleteMessage":  (lambda a: g.delete_message(a[0], a[1], bool(a[2] if len(a) > 2 else False)), "write"),
        "reactToMessage": (lambda a: g.react(a[0], a[1], a[2]), "write"),
        "forwardMessages": (lambda a: g.forward(a[0], list(a[1] or []), a[2]), "write"),
        "votePoll":       (lambda a: g.vote_poll(a[0], a[1], int(a[2])), "write"),
        "pinMessage":     (lambda a: g.pin_message(a[0], a[1]), "write"),
        "markRead":       (lambda a: g.mark_read(a[0]), "write"),
        "setTyping":      (lambda a: g.set_typing(a[0]), "write"),
        "saveDraft":      (lambda a: g.save_draft(a[0], a[1] or ""), "write"),
        "pinChat":        (lambda a: g.pin_chat(a[0]), "write"),
        "muteChat":       (lambda a: g.mute_chat(a[0]), "write"),
        "archiveChat":    (lambda a: g.archive_chat(a[0]), "write"),
        "clearHistory":   (lambda a: g.clear_history(a[0]), "write"),
        "deleteChat":     (lambda a: g.disable_chat(a[0]), "write"),
        "createChat":     (lambda a: g.create_chat(a[0], a[1], list(a[2] or [])), "write"),
        "addMembers":     (lambda a: g.add_members(a[0], list(a[1] or [])), "write"),
        "setPermission":  (lambda a: g.set_permission(a[0], a[1], bool(a[2])), "write"),
        "setSignMessages": (lambda a: g.set_sign_messages(a[0], bool(a[1])), "write"),
        "blockUser":      (lambda a: g.block_user(a[0]), "write"),
        "unblockUser":    (lambda a: g.unblock_user(a[0]), "write"),
        "updateSettings": (lambda a: g.update_settings(a[0] or {}), "write"),
        "updateProfile":  (lambda a: g.update_profile(a[0] or {}), "write"),
        "terminateSession": (lambda a: g.terminate_session(str(a[0])), "write"),
    }


@app.post("/api/rpc")
async def rpc(request: Request):
    session = await security.load_session(request)
    if not session:
        return _err(NO_SESSION_ERROR, status=401)
    if not security.origin_ok(request):
        return _err("That request did not come from this site.", status=403)
    if request.headers.get("x-csrf-token") != session.csrf:
        return _err("This tab is out of date. Reload the page.", status=403)
    if GW is None:
        return _err("The Telegram bridge is still starting up. Try again in a moment.")

    try:
        body = await request.json()
    except Exception:
        return _err("Malformed request.", status=400)
    method = str(body.get("method") or "")
    args = body.get("args") or []
    if not isinstance(args, list):
        return _err("Malformed request.", status=400)

    table = await _method_table()
    entry = table.get(method)
    if entry is None:
        return _err("That action is not available.", status=404)
    fn, klass = entry
    if not security.allow(session.id, klass):
        return _err(security.RATE_LIMIT_MESSAGE, status=429)

    try:
        data = await fn(args)
    except UserFacing as e:
        return _err(str(e))
    except IndexError:
        return _err("That action was called without everything it needs.", status=400)
    except ValueError as e:
        log.warning("rpc %s rejected: %s", method, e)
        return _err("That value was not something Telegram would accept.")
    except Exception:
        log.exception("rpc %s failed", method)
        return _err(GENERIC_ERROR)
    return _ok(_sign_urls(data, session.id))


@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    """Synchronous-friendly boot payload.

    api.js reads this before first paint because the frontend calls API.me() and
    API.userById() synchronously while rendering.
    """
    session = await security.load_session(request)
    if not session:
        return _err(NO_SESSION_ERROR, status=401)
    if GW is None:
        return _err("The Telegram bridge is still starting up.")
    try:
        data = await GW.bootstrap()
    except UserFacing as e:
        return _err(str(e))
    except Exception:
        log.exception("bootstrap failed")
        return _err(GENERIC_ERROR)
    data["csrf"] = session.csrf
    data["handlers"] = list(HANDLER_NAMES)
    return _ok(_sign_urls(data, session.id))


@app.post("/api/logout")
async def api_logout(request: Request):
    """The client's "Log out" only flips local state, so api.js routes it here."""
    session = await security.load_session(request)
    if session and request.headers.get("x-csrf-token") != session.csrf:
        return _err("This tab is out of date. Reload the page.", status=403)
    await security.destroy_session(request)
    response = _ok({"loggedOut": True})
    response.delete_cookie(CFG.cookie_name, path="/")
    response.delete_cookie("tg_csrf", path="/")
    return response


# ------------------------------------------------------------------- upload

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile | None = None):
    session = await security.load_session(request)
    if not session:
        return _err(NO_SESSION_ERROR, status=401)
    if not security.origin_ok(request):
        return _err("That request did not come from this site.", status=403)
    if request.headers.get("x-csrf-token") != session.csrf:
        return _err("This tab is out of date. Reload the page.", status=403)
    if not security.allow(session.id, "upload"):
        return _err(security.RATE_LIMIT_MESSAGE, status=429)
    if file is None:
        return _err("No file was attached to that upload.")

    handle = "up_" + secrets.token_urlsafe(16)
    rel = f"{handle}.bin"
    dest = CFG.upload_dir / rel
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 256):
                written += len(chunk)
                if written > CFG.upload_max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return _err(
                        f"That file is larger than the {media.human_size(CFG.upload_max_bytes)} limit."
                    )
                out.write(chunk)
        os.chmod(dest, 0o600)
    except OSError:
        log.exception("upload write failed")
        dest.unlink(missing_ok=True)
        return _err("The upload could not be saved on the server.")

    name = Path(file.filename or "upload.bin").name[:120]
    await db.execute(
        "INSERT INTO upload_handles(handle, rel_path, name, size, mime, synthetic, created_at)"
        " VALUES(?,?,?,?,?,0,?)",
        (handle, rel, name, written, file.content_type or "application/octet-stream", db.now_ms()),
    )
    return _ok({
        "url": f"/media/upload/{handle}",
        "name": name,
        "size": media.human_size(written),
        "mime": file.content_type or "application/octet-stream",
    })


# -------------------------------------------------------------------- media

_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@app.get("/media/{rest:path}")
async def media_route(rest: str, request: Request, exp: int = 0, sig: str = ""):
    session = await security.load_session(request)
    if not session:
        return PlainTextResponse("Sign in first.", status_code=401)
    if not security.allow(session.id, "media"):
        return PlainTextResponse(security.RATE_LIMIT_MESSAGE, status_code=429)
    path = f"/media/{rest}"
    if not verify_media(path, int(exp or 0), session.id, sig):
        # Wrong, expired, or a URL minted for a different login session.
        return PlainTextResponse("This media link is not valid.", status_code=403)

    parts = rest.split("/")
    try:
        if parts[0] == "upload" and len(parts) == 2:
            return await _serve_upload(parts[1])
        if parts[0] == "avatar" and len(parts) == 4:
            return await _serve_avatar(parts[1], parts[3])
        if len(parts) == 3:
            return await _serve_message_media(parts[0], parts[1], parts[2])
    except UserFacing as e:
        return PlainTextResponse(str(e), status_code=404)
    except Exception:
        log.exception("media fetch failed for %s", path)
        return PlainTextResponse("That attachment could not be fetched.", status_code=502)
    return PlainTextResponse("Not found", status_code=404)


async def _serve_upload(handle: str) -> Response:
    if not _ID_OK.match(handle):
        return PlainTextResponse("Not found", status_code=404)
    row = await db.fetchone("SELECT rel_path, name, mime FROM upload_handles WHERE handle=?", (handle,))
    if not row or not row["rel_path"]:
        return PlainTextResponse("That upload has expired.", status_code=404)
    return _file_response(CFG.upload_dir / row["rel_path"], row["mime"], row["name"])


async def _serve_avatar(entity_id: str, variant: str) -> Response:
    if variant not in ("thumb", "full"):
        return PlainTextResponse("Not found", status_code=404)
    key = media.key_for("avatar", entity_id, variant)
    path, mime = await media.get_or_fetch(key, lambda: GW.fetch_avatar(entity_id, variant))
    return _file_response(path, mime, None)


async def _serve_message_media(chat_id: str, message_id: str, variant: str) -> Response:
    if variant not in ("thumb", "full", "file", "voice"):
        return PlainTextResponse("Not found", status_code=404)
    key = media.key_for("msg", chat_id, message_id, variant)
    path, mime = await media.get_or_fetch(
        key, lambda: GW.fetch_media(chat_id, message_id, variant)
    )
    filename = None
    if variant == "file":
        filename = Path(str(path)).name
    return _file_response(path, mime, filename)


def _file_response(path: Path, mime: str | None, filename: str | None) -> Response:
    path = Path(path)
    if not path.is_file():
        return PlainTextResponse("That attachment is no longer cached.", status_code=404)
    content_type, force_attachment = media.safe_mime(mime or "")
    headers = {
        "Cache-Control": "private, max-age=600",
        "X-Content-Type-Options": "nosniff",
    }
    disposition = "attachment" if (force_attachment or filename) else "inline"
    return FileResponse(
        path,
        media_type=content_type,
        headers=headers,
        filename=filename if disposition == "attachment" else None,
    )


# ----------------------------------------------------------------- websocket

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    # Origin is checked before the handshake completes: SameSite does not
    # protect websockets.
    origin = (ws.headers.get("origin") or "").rstrip("/")
    host = ws.headers.get("host", "")
    allowed = CFG.allowed_origins or (f"http://{host}", f"https://{host}")
    if origin and origin not in allowed:
        await ws.close(code=4403)
        return
    token = ws.cookies.get(CFG.cookie_name)
    if not token:
        await ws.close(code=4401)
        return

    class _Shim:  # load_session takes a Request-like object
        cookies = ws.cookies
        headers = ws.headers
        client = ws.client

    session = await security.load_session(_Shim())  # type: ignore[arg-type]
    if not session:
        await ws.close(code=4401)
        return

    await ws.accept()
    queue = HUB.register()
    try:
        await ws.send_text(json.dumps({"event": "onConnectionChange",
                                       "payload": {"status": HUB.status}}))
        while True:
            frame = await queue.get()
            payload = _sign_urls(frame["payload"], session.id)
            await ws.send_text(json.dumps({"event": frame["event"], "payload": payload}))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        HUB.unregister(queue)


# --------------------------------------------------------------- dev helpers

@app.post("/api/_dev/{action}")
async def dev_action(action: str, request: Request):
    """Only present with the fake gateway: lets .addweb / .delweb be exercised."""
    if GW is None or GW.name != "fake":
        return _err("Not available.", status=404)
    session = await security.load_session(request)
    if not session:
        return _err(NO_SESSION_ERROR, status=401)
    if action == "addweb":
        return _ok(_sign_urls(await GW.simulate_addweb(), session.id))
    if action == "delweb":
        return _ok(await GW.simulate_delweb())
    return _err("Unknown action.", status=404)



# ------------------------------------------------------------------- static
# Registered last so it cannot shadow /api/* or /media/*.

_STATIC_OK = re.compile(r"^[A-Za-z0-9._/\-]+$")


@app.get("/{asset:path}")
async def static_asset(asset: str, request: Request):
    """Serves the untouched frontend files. Auth-gated like everything else."""
    if not asset or not _STATIC_OK.match(asset) or ".." in asset:
        return PlainTextResponse("Not found", status_code=404)
    if not await security.load_session(request):
        return RedirectResponse("/login", status_code=302)
    target = (CFG.frontend_dir / asset).resolve()
    try:
        target.relative_to(CFG.frontend_dir.resolve())
    except ValueError:
        return PlainTextResponse("Not found", status_code=404)
    if not target.is_file():
        return PlainTextResponse("Not found", status_code=404)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mime, headers={"Cache-Control": "no-store"})

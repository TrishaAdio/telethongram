# Telethongram bridge

Backend for the finished Telethongram frontend: an MTProto client running as your
own Telegram account, behind an HTTP + WebSocket bridge the browser talks to.
Chats appear only after you type `.addweb` in them; `.delweb` removes them.

`api.js` is the only frontend file that changed. `Messenger.dc.html`,
`support.js` and `_ds/` are byte-identical to the originals.

## Layout

```
backend/app/
  main.py            HTTP + WebSocket surface, auth gate, media route, RPC dispatch
  security.py        argon2 login, cookie sessions, CSRF, origin checks, rate limits
  crypto.py          session encryption at rest, HMAC media-URL signing
  floodqueue.py      FLOOD_WAIT scheduler: per-peer ordering, backoff, two lanes
  media.py           lazy download, single-flight, LRU eviction, waveform decode
  ids.py             client id <-> int64 mapping, strict int64 parsing
  db.py              SQLite (WAL) schema
  settings_store.py  server-owned settings
  hub.py             websocket fan-out
  gateway/
    base.py          the seam: everything Telegram-shaped is behind this
    telethon_gw.py   the real MTProto implementation
    fake.py          in-memory stand-in, so the whole bridge is testable with no account
backend/cli.py       keygen / hash / login / check / logout-all
frontend/            the untouched client + the rewritten api.js
deploy/              systemd unit, Caddyfile, env.example
scripts/smoke.sh     75 end-to-end checks against the fake gateway
```

## Install on the VPS

```bash
sudo useradd --system --home /opt/telethongram telethongram
sudo git clone <this repo> /opt/telethongram && cd /opt/telethongram
sudo python3.11 -m venv .venv && sudo .venv/bin/pip install -r backend/requirements.txt
sudo install -d -o telethongram -g telethongram -m 700 /var/lib/telethongram
sudo install -d -m 750 /etc/telethongram
sudo cp deploy/env.example /etc/telethongram/env && sudo chmod 640 /etc/telethongram/env
```

Fill in the env file:

```bash
.venv/bin/python -m backend.cli keygen   # -> SECRET_KEY
.venv/bin/python -m backend.cli hash     # -> WEB_PASSWORD_HASH
```

Sign in to Telegram once, over SSH — never from the browser:

```bash
sudo -u telethongram env $(grep -v '^#' /etc/telethongram/env | xargs) \
  .venv/bin/python -m backend.cli login
```

That writes `/var/lib/telethongram/session.enc` (AES-256-GCM, 0600). The key is
`SECRET_KEY` in the env file; the ciphertext is useless without it.

```bash
sudo cp deploy/telethongram.service /etc/systemd/system/
sudo systemctl enable --now telethongram
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the hostname first
sudo systemctl reload caddy
```

The bridge listens on `127.0.0.1:4000` and Caddy terminates TLS in front of it.
HTTPS is not optional: the session cookie and every message body cross that hop,
and the cookie is set `Secure`, so plain HTTP will not authenticate at all.

### No domain name?

Pick one of these rather than serving plain HTTP over an IP.

**A free hostname, so you get a real certificate.** Register any subdomain
(DuckDNS and similar are free), point it at the VPS, and use `deploy/Caddyfile`
unchanged. Port 80 must be reachable for the ACME challenge. This is the only
option with no browser warning.

**An SSH tunnel — nothing is exposed at all.** Leave `HOST=127.0.0.1`,
`PORT=4000`, set `COOKIE_SECURE=false` and `ALLOWED_ORIGINS=` (empty), then from
your laptop:

```bash
ssh -N -L 4000:127.0.0.1:4000 you@your-vps
```

Open `http://localhost:4000`. The traffic is inside SSH, and the bridge is not
listening on any public interface.

**HTTPS on the IP with a self-signed certificate.** Use `deploy/Caddyfile.ip`,
set `PORT=4001` so Caddy can own 4000, and `ALLOWED_ORIGINS=https://YOUR.IP:4000`.
One browser warning to accept; encryption and `Secure` cookies both keep working.

Plain `http://IP:4000` requires `COOKIE_SECURE=false`, which means your login
cookie and every message you read travel unencrypted. Only do it on a network you
control, and never leave it running that way.

## Decisions baked in

Each of these is a single env flag away from the opposite behaviour.

| Area | Default | Flag |
| --- | --- | --- |
| Users | Single user (you). Web sign-in is a passphrase, not phone/OTP | — |
| `deleteChat` | Sets `enabled:false` only. Never leaves or deletes a Telegram chat | — |
| `clearHistory` | Hides history in this client only (a per-chat watermark) | `DESTRUCTIVE_CLEAR_HISTORY` |
| `.addweb` message | Filtered from history and the update stream, left in Telegram | `DELETE_COMMAND_MESSAGES` |
| Command match | Whole message, case-insensitive: `.addweb` / `.delweb` | `COMMAND_PREFIX` |
| Pin / mute / archive | Change real Telegram dialog state | `MIRROR_DIALOG_STATE` |
| Drafts | Stored on the bridge, not synced to your phone | `TELEGRAM_DRAFTS` |
| Saved Messages | Always enabled — the UI's Forward action targets it by id | `AUTO_ENABLE_SAVED` |
| `twoStep` | Reported from Telegram, never written (the UI cannot collect a password) | — |

## Contract details worth knowing

- **int64.** `tgChatId` and `tgMessageId` are always JSON **strings**. The frontend
  never does arithmetic on them (verified: they appear nowhere in
  `Messenger.dc.html`), so precision cannot be lost. Internally they are Python
  ints and SQLite `INTEGER`s, parsed with a strict range check, never via float.
- **Three ids are hard-coded in the UI** and are honoured exactly: `u_me` (self,
  used for "did I react"), `c_saved` (the Forward target), and `c_city` (the
  desktop boot chat, aliased in `api.js` to whichever chat sorts first).
- **`API.me()` and `API.userById()` are synchronous** in the shell's render path,
  so `api.js` fetches `/api/bootstrap` once at load and keeps a local cache the
  socket refreshes.
- **Optimistic sends** are de-duplicated in `api.js`: an outgoing message painted
  from the `sendMessage` response is dropped when its socket echo arrives.
- **Command filtering is entirely server-side.** `.addweb` and `.delweb` are
  recorded in `filtered_msgs` before any handler fires and excluded from history,
  search, shared media and the chat-list preview. The frontend has no awareness
  of commands.
- **The browser never talks to Telegram.** Media is proxied and cached; the client
  only ever sees `/media/...` URLs on our origin, signed with HMAC-SHA256 and
  bound to `exp` + the login session, so they cannot be enumerated or shared.
- **Uploads** go browser → `/api/upload` → disk → `sendMessage`. The shell hands
  `uploadFile` a descriptor rather than a real `File`, so `api.js` opens a native
  file picker and uploads actual bytes; XHR upload events drive the progress bar.
- **FLOOD_WAIT** is absorbed server-side: one scheduler, per-peer ordering, two
  lanes so media downloads cannot delay a send, and a human-readable error when
  the wait is longer than five minutes.
- **Nothing secret is logged.** A redaction filter scrubs session strings, API
  hashes, tokens and long base64 blobs as a backstop; message bodies are never
  logged at all.

## The CSP compromise, stated plainly

`support.js` fetches React, ReactDOM and **Babel standalone** from `unpkg.com` at
runtime and compiles the page's inline script with `new Function`. So the policy
has to include `'unsafe-eval'` and that third-party origin, or the app renders a
blank page. Both are real weaknesses on a page displaying private messages: a
compromised unpkg response would execute in our origin with the session cookie.

Fixing it properly means vendoring those three files and pointing `support.js` at
local copies — a one-line change to a file the brief says not to touch, so it is
your call. `'unsafe-eval'` cannot be removed at all while the page is compiled in
the browser at load time.

## Verify locally

```bash
bash scripts/smoke.sh
```

Starts the bridge with `GATEWAY=fake` (no Telegram account involved) and runs 75
checks: auth gating, CSRF, origin rejection, brute-force throttling, the contract
shapes, int64-as-string, media signing and tampering, upload limits, the
websocket, `.addweb`/`.delweb` fan-out, rate limiting, and that no secret reached
the log. All 75 pass.

## What is not verified here

The Telethon gateway itself needs your `TG_API_ID`, `TG_API_HASH` and a real
sign-in, so it has been reviewed and type-checked but not executed. The first run
on your VPS is the real test of the MTProto half — set `LOG_LEVEL=DEBUG` for it.

Also worth knowing before you spend time on media: the current shell renders
photos as a CSS placeholder, avatars as generated initials, and "download" as a
toast. It accepts the URLs but never paints the bytes, so the proxy is verified
with `curl`, not with your eyes.

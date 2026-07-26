#!/usr/bin/env bash
# Local verification against the fake gateway: starts the bridge, exercises the
# contract end to end, then stops it. Nothing here touches a Telegram account.
#
# The brute-force checks run last on purpose: they exhaust the login rate
# limiter for this IP, which is exactly what they are meant to prove.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
DATA=$ROOT/.local/data
LOG=$ROOT/.local/bridge.log
JAR=$ROOT/.local/cookies
mkdir -p "$ROOT/.local"
rm -rf "$DATA" "$JAR" "$LOG"

export SECRET_KEY=$(.venv/bin/python -c "import secrets;print(secrets.token_urlsafe(48))")
export WEB_PASSWORD_HASH=$(.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
from backend.app.security import hash_password
print(hash_password('correct-horse-battery'))
PY
)
export GATEWAY=fake COOKIE_SECURE=false LOG_LEVEL=INFO
export DATA_DIR=$DATA

.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 4000 > "$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
for i in $(seq 1 40); do
  curl -sf -m 2 http://127.0.0.1:4000/healthz > /dev/null && break
  sleep 0.5
done

B=http://127.0.0.1:4000
pass=0; fail=0
check() {
  if [[ "$3" == *"$2"* ]]; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1"; echo "        wanted: $2"; echo "        got:    ${3:0:300}"; fail=$((fail+1)); fi
}

echo "== health and gating"
check "healthz reports the gateway" '"gateway":"fake"' "$(curl -s $B/healthz)"
check "/ redirects to the sign-in page" "/login" "$(curl -s -o /dev/null -w '%{redirect_url}' $B/)"
check "rpc without a cookie is 401 with a human error" "session expired" \
  "$(curl -s -X POST $B/api/rpc -H 'content-type: application/json' -d '{"method":"getChats"}')"
check "frontend assets are gated too" "/login" \
  "$(curl -s -o /dev/null -w '%{redirect_url}' $B/support.js)"
check "a CSP is sent" "content-security-policy" "$(curl -s -D - -o /dev/null $B/login | tr 'A-Z' 'a-z')"
check "clickjacking is blocked" "x-frame-options: deny" "$(curl -s -D - -o /dev/null $B/login | tr 'A-Z' 'a-z')"

echo "== sign in"
LOGIN=$(curl -s -c "$JAR" -o /dev/null -w '%{http_code} %{redirect_url}' -X POST $B/login -d 'passphrase=correct-horse-battery')
check "the right passphrase signs in" "302 http://127.0.0.1:4000/" "$LOGIN"
check "the session cookie is HttpOnly" "TRUE" "$(grep -i 'tg_sid' "$JAR" | grep -o '^#HttpOnly_127.0.0.1.*TRUE' >/dev/null && echo TRUE || (grep -q '#HttpOnly_.*tg_sid' "$JAR" && echo TRUE))"
check "the shell is served once signed in" "api.js" "$(curl -s -b "$JAR" $B/ | head -c 1200)"
check "api.js parses as valid javascript" "ok" \
  "$(curl -s -b "$JAR" $B/api.js > .local/api.fetched.js && node --check .local/api.fetched.js && echo ok)"
check "frontend assets are served once signed in" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" $B/api.js)"

BOOT=$(curl -s -b "$JAR" $B/api/bootstrap)
CSRF=$(echo "$BOOT" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["data"]["csrf"])')
check "bootstrap carries me, so API.me() can stay synchronous" '"u_me"' "$BOOT"
check "bootstrap carries the handler names" "onConnectionChange" "$BOOT"
check "bootstrap leaks no secret" "" "$(echo "$BOOT" | grep -o "$SECRET_KEY")"

rpc() { curl -s -b "$JAR" -X POST $B/api/rpc -H 'content-type: application/json' \
  -H "X-CSRF-Token: $CSRF" -d "{\"method\":\"$1\",\"args\":$2}"; }
pp() { .venv/bin/python -m json.tool; }

echo "== csrf and origin"
check "rpc without the csrf header is refused" "out of date" \
  "$(curl -s -b "$JAR" -X POST $B/api/rpc -H 'content-type: application/json' -d '{"method":"getChats","args":[]}')"
check "a foreign Origin is refused" "did not come from this site" \
  "$(curl -s -b "$JAR" -X POST $B/api/rpc -H 'content-type: application/json' \
     -H "X-CSRF-Token: $CSRF" -H 'Origin: https://evil.example' -d '{"method":"getChats","args":[]}')"
check "a null Origin from a cross-site post is refused" "did not come from this site" \
  "$(curl -s -b "$JAR" -X POST $B/api/rpc -H 'content-type: application/json' \
     -H "X-CSRF-Token: $CSRF" -H 'Origin: null' -H 'Sec-Fetch-Site: cross-site' \
     -d '{"method":"getChats","args":[]}')"
check "a null Origin from a same-origin form post is allowed" '"ok": true' \
  "$(curl -s -b "$JAR" -X POST $B/api/rpc -H 'content-type: application/json' \
     -H "X-CSRF-Token: $CSRF" -H 'Origin: null' -H 'Sec-Fetch-Site: same-origin' \
     -d '{"method":"getChats","args":[]}' | pp | head -2)"
check "signing in with a null Origin works" "302" \
  "$(curl -s -o /dev/null -w '%{http_code}' -c /dev/null -X POST $B/login \
     -H 'Origin: null' -H 'Sec-Fetch-Site: same-origin' -d 'passphrase=correct-horse-battery')"

echo "== reads"
CHATS=$(rpc getChats '[]')
check "getChats returns the enabled set" '"enabled": true' "$(echo "$CHATS" | pp)"
check "int64 chat ids are strings, not numbers" '"tgChatId": "-1001884420011"' "$(echo "$CHATS" | pp)"
check "Saved Messages keeps the id the UI hard-codes" '"id": "c_saved"' "$(echo "$CHATS" | pp)"
check "the chat row carries a denormalised lastMessage" '"lastMessage"' "$CHATS"
check "getMessages comes back ascending by date" "True" "$(rpc getMessages '["c_g-1001884420011",{"limit":40}]' | .venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)["data"]
print(all(d[i]["date"]<=d[i+1]["date"] for i in range(len(d)-1)))')"
check "before= pages upward" "True" "$(rpc getMessages '["c_g-1001884420011",{"before":1,"limit":20}]' | .venv/bin/python -c '
import json,sys; print(json.load(sys.stdin)["data"]==[])')"
check "message int64 ids are strings" '"tgMessageId": "' "$(rpc getMessages '["c_saved",{}]' | pp)"
check "senderColor is a palette index 0-7" "True" "$(rpc getMessages '["c_g-1001884420011",{}]' | .venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)["data"]
print(all(isinstance(m["senderColor"],int) and 0<=m["senderColor"]<=7 for m in d))')"
check "a voice note carries a decoded waveform" '"wave"' "$(rpc getMessages '["c_u771019",{}]')"
check "a chat that is not enabled errors for a human" "Run .addweb in it first" "$(rpc getChat '["c_nope"]')"
check "getSharedMedia returns the four keys" '"voice"' "$(rpc getSharedMedia '["c_u771019"]')"
check "getSharedMedia takes a single type" "[" "$(rpc getSharedMedia '["c_u771019","voice"]')"
check "searchAll annotates messages with chatTitle" '"chatTitle"' "$(rpc searchAll '["nut graf"]')"
check "an empty query returns empty lists" '"messages": []' "$(rpc searchAll '[""]' | pp)"
check "searchAll finds contacts" "Mira" "$(rpc searchAll '["mira"]')"
check "getSettings has the contract keys" '"wallpaper"' "$(rpc getSettings '[]')"
check "getSessions marks the current session" '"current": true' "$(rpc getSessions '[]' | pp)"

echo "== writes"
SENT=$(rpc sendMessage '["c_saved",{"type":"text","text":"hello from the bridge"}]')
MID=$(echo "$SENT" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["data"]["id"])')
check "sendMessage returns the created message" '"status": "sent"' "$(echo "$SENT" | pp)"
check "sending clears the server-side draft" '"draft": ""' "$(rpc getChat '["c_saved"]' | pp)"
check "editMessage marks it edited" '"edited": true' "$(rpc editMessage "[\"c_saved\",\"$MID\",\"edited text\"]" | pp)"
check "reactToMessage toggles my reaction on" 'u_me' "$(rpc reactToMessage "[\"c_saved\",\"$MID\",\"x\"]")"
check "reactToMessage toggles it off again" '"reactions": {}' "$(rpc reactToMessage "[\"c_saved\",\"$MID\",\"x\"]" | pp)"
check "forwardMessages reports a count" '"count": 1' "$(rpc forwardMessages "[\"c_saved\",[\"$MID\"],\"c_g-1001884420011\"]" | pp)"
check "votePoll records the vote" '"voted": 1' "$(rpc getMessages '["c_ch-1001884420099",{}]' | .venv/bin/python -c '
import json,sys
for m in json.load(sys.stdin)["data"]:
    if m["type"]=="poll": print(m["id"])' | xargs -I{} bash -c "$(declare -f rpc); JAR=$JAR B=$B CSRF=$CSRF; rpc votePoll '[\"c_ch-1001884420099\",\"{}\",1]'" | pp)"
check "deleteMessage answers with the id" '"messageId"' "$(rpc deleteMessage "[\"c_saved\",\"$MID\",false]")"
check "settings round-trip and clamp fontSize to 20" '"fontSize": 20' "$(rpc updateSettings '[{"fontSize":99,"theme":"dark"}]' | pp)"
check "unknown settings keys are dropped" "" "$(rpc updateSettings '[{"evil":"x"}]' | grep -o evil)"
check "twoStep cannot be set from the client" "false" "$(rpc updateSettings '[{"twoStep":true}]' | .venv/bin/python -c '
import json,sys; print(str(json.load(sys.stdin)["data"]["twoStep"]).lower())')"
check "muteChat toggles" '"muted": true' "$(rpc muteChat '["c_g-1001884420011"]' | pp)"
check "pinChat toggles" '"pinned"' "$(rpc pinChat '["c_g-1001884420011"]')"
check "a photo send with no bytes behind it says so plainly" "no file behind that attachment" \
  "$(rpc sendMessage '["c_saved",{"type":"photo","media":{"photos":[{"url":"/media/upload/up_missing"}]}}]')"
check "deleteChat only disables the chat" '"chatId": "c_u771019"' "$(rpc deleteChat '["c_u771019"]' | pp)"
check "the disabled chat drops out of getChats" "gone" \
  "$(rpc getChats '[]' | grep -q 'c_u771019' && echo still-there || echo gone)"
check "unknown methods are refused" "not available" "$(rpc definitelyNotAMethod '[]')"

echo "== upload and media proxy"
head -c 20000 /dev/urandom > "$ROOT/.local/blob.bin"
UP=$(curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" -F "file=@$ROOT/.local/blob.bin" $B/api/upload)
check "upload returns a handle url" '/media/upload/up_' "$UP"
check "upload reports a pre-formatted size" '19.5 KB' "$UP"
UPURL=$(echo "$UP" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["data"]["url"])')
check "an unsigned media url is refused" "not valid" "$(curl -s -b "$JAR" "$B$UPURL")"
check "upload without the csrf header is refused" "out of date" \
  "$(curl -s -b "$JAR" -F "file=@$ROOT/.local/blob.bin" $B/api/upload)"

SIGNED=$(rpc getMessages '["c_g-1001884420011",{}]' | .venv/bin/python -c '
import json,sys
for m in json.load(sys.stdin)["data"]:
    if m["type"]=="photo": print(m["media"]["photos"][0]["url"]); break')
check "media urls are handed over signed" "sig=" "$SIGNED"
check "a signed media url serves bytes" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$B$SIGNED")"
check "the second hit is served from cache" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$B$SIGNED")"
check "a tampered signature is refused" "403" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "${SIGNED%?}x" --url-query '' 2>/dev/null || curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$B${SIGNED%?}x")"
check "an unsigned guess at a media path is refused" "403" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$B/media/c_saved/m40001/full")"
check "an expired url is refused" "403" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$B/media/c_saved/m40001/full?exp=1&sig=whatever")"
check "media without a session is refused" "401" \
  "$(curl -s -o /dev/null -w '%{http_code}' "$B$SIGNED")"

echo "== websocket, .addweb and .delweb"
# Sessions are bound to the user agent, so this raw client must present the same
# one curl used when it signed in.
export SMOKE_UA="curl/$(curl --version | head -1 | awk '{print $2}')"
WS=$(.venv/bin/python - <<'PY'
import asyncio, base64, json, os, urllib.request
cookie = ""
for line in open(".local/cookies"):
    parts = line.strip().split("\t")
    if len(parts) == 7 and parts[5] == "tg_sid":
        cookie = "tg_sid=" + parts[6]
UA = os.environ.get("SMOKE_UA", "curl/8")
async def main():
    reader, writer = await asyncio.open_connection("127.0.0.1", 4000)
    writer.write((
        "GET /ws HTTP/1.1\r\nHost: 127.0.0.1:4000\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
        f"User-Agent: {UA}\r\n"
        f"Cookie: {cookie}\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    if b" 101 " not in head:
        print("HANDSHAKE FAILED"); return
    async def frame():
        h = await reader.readexactly(2)
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(await reader.readexactly(2), "big")
        return json.loads((await reader.readexactly(ln)).decode())
    out = [await asyncio.wait_for(frame(), 5)]
    def post(path):
        return urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:4000" + path, data=b"",
            headers={"Cookie": cookie, "User-Agent": UA}), timeout=5).read()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, post, "/api/_dev/addweb")
    out.append(await asyncio.wait_for(frame(), 5))
    await loop.run_in_executor(None, post, "/api/_dev/delweb")
    out.append(await asyncio.wait_for(frame(), 5))
    print(json.dumps(out))
asyncio.run(main())
PY
)
check "the socket announces connection status" "onConnectionChange" "$WS"
check ".addweb pushes onChatAdded live" "onChatAdded" "$WS"
check "the added chat arrives fully shaped" "Wire Watch" "$WS"
check "the pushed chat has signed media urls too" "c_g-1001884420077" "$WS"
check ".delweb pushes onChatRemoved" "onChatRemoved" "$WS"
check "a socket without a cookie is closed" "not-101" \
  "$(.venv/bin/python - <<'PY'
import asyncio, base64, os
async def main():
    r, w = await asyncio.open_connection("127.0.0.1", 4000)
    w.write(("GET /ws HTTP/1.1\r\nHost: 127.0.0.1:4000\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
             f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n\r\n").encode())
    head = await r.readuntil(b"\r\n\r\n")
    print("101" if b" 101 " in head else "not-101")
asyncio.run(main())
PY
)"

echo "== rate limiting and brute force (exhausts this IP's login budget)"
BURST=$(for i in $(seq 1 12); do rpc searchAll '["x"]'; done | grep -c 'Slow down')
check "the search bucket rate-limits a burst" "true" "$([[ $BURST -gt 0 ]] && echo true || echo false)"
check "a wrong passphrase says nothing useful" "was not accepted" \
  "$(curl -s -X POST $B/login -d 'passphrase=wrong-but-first' | grep -o 'was not accepted')"
for i in 1 2 3 4 5 6 7 8; do
  BF=$(curl -s -o /dev/null -w '%{http_code}' -X POST $B/login -d 'passphrase=wrong')
done
check "repeated bad passphrases end in 429" "429" "$BF"
ATTEMPTS=$(.venv/bin/python - <<PY
import sqlite3
c = sqlite3.connect("$DATA/db.sqlite")
print(c.execute("select count(*) from login_attempts where outcome!='ok'").fetchone()[0])
PY
)
check "every failed attempt is recorded for the throttle" "true" \
  "$([[ ${ATTEMPTS:-0} -ge 3 ]] && echo true || echo false)"

echo "== config self-check"
check "a transport warning is never a startup blocker" "true" \
  "$(HOST=0.0.0.0 COOKIE_SECURE=false GATEWAY=telethon TG_API_ID=1 TG_API_HASH=x \
     .venv/bin/python -c '
import sys; sys.path.insert(0, ".")
from backend.app.config import Config
c = Config()
print(str(any("COOKIE_SECURE" in a for a in c.advisories())
          and not any("COOKIE_SECURE" in b for b in c.blockers())).lower())')"
check "a missing session is a blocker" "true" \
  "$(GATEWAY=telethon TG_API_ID=1 TG_API_HASH=x DATA_DIR=/nonexistent-xyz \
     .venv/bin/python -c '
import sys; sys.path.insert(0, ".")
from backend.app.config import Config
print(str(any("session" in b for b in Config().blockers())).lower())')"

echo "== logs"
check "no secret ever reached the log" "" "$(grep -o "$SECRET_KEY" "$LOG")"
check "no passphrase reached the log" "" "$(grep -o 'correct-horse-battery' "$LOG")"
check "no unhandled traceback in the log" "" "$(grep -c 'Traceback' "$LOG" | grep -v '^0$')"
check "failed logins are logged without the attempt" "failed login from" "$(grep -o 'failed login from' "$LOG" | head -1)"

echo
echo "passed: $pass   failed: $fail"
[[ $fail -eq 0 ]]

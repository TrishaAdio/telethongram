"""In-memory gateway.

Exists so the HTTP tier, the websocket, all four view states, auth, rate limits,
media signing and eviction can be exercised without touching a real Telegram
account. Nothing in api.js knows it exists; it speaks the same contract shapes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import db, ids, media, settings_store
from ..hub import HUB
from .base import Gateway, UserFacing

DAY = 86_400_000


def _now() -> int:
    return int(time.time() * 1000)


class FakeGateway(Gateway):
    name = "fake"

    def __init__(self) -> None:
        n = _now()
        self.me = {
            "id": ids.ME, "tgUserId": "771002", "name": "Rosalind Hale", "username": "@rhale",
            "phone": "+1 202 555 0148", "bio": "Deputy editor, City desk.", "initials": "RH",
            "avatarUrl": None, "online": True, "lastSeen": None, "blocked": False,
        }
        self.users: dict[str, dict] = {ids.ME: self.me}
        for uid, name, uname, tg in (
            ("u771019", "Mira Okonkwo", "@mira", 771019),
            ("u771077", "Anton Reyes", "@areyes", 771077),
            ("u771002b", "June Ashworth", "@june", 771003),
        ):
            self.users[uid] = {
                "id": uid, "tgUserId": str(tg), "name": name, "username": uname,
                "phone": None, "bio": "", "initials": ids.initials(name), "avatarUrl": None,
                "online": tg % 2 == 1, "lastSeen": n - 3600_000, "blocked": False,
            }
        self.chats: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.blocked: list[str] = []
        self.seq = 40_000
        self._seed()

    # ------------------------------------------------------------------ seed
    def _seed(self) -> None:
        n = _now()
        self._add_chat(ids.SAVED, "saved", "Saved Messages", 771002, pinned=True, added=n - 30 * DAY)
        self._add_chat("c_g-1001884420011", "group", "Desk — City", -1001884420011,
                       pinned=True, unread=2, added=n - 21 * DAY,
                       members=[ids.ME, "u771019", "u771077", "u771002b"])
        self._add_chat("c_u771019", "private", "Mira Okonkwo", 771019,
                       added=n - 19 * DAY, user="u771019")
        self._add_chat("c_ch-1001884420099", "channel", "The Column", -1001884420099,
                       unread=4, added=n - 17 * DAY, member_count=12480, sign=True)
        self._msg("c_g-1001884420011", "u771002b", n - 2 * DAY, "text",
                  text="Budget meeting moved to 10:30.")
        self._msg("c_g-1001884420011", ids.ME, n - DAY, "text",
                  text="Filed the nut graf.", reactions={"👍": ["u771019"]})
        self._msg("c_g-1001884420011", "u771019", n - 3600_000, "photo",
                  media={"photos": [{"url": "/media/c_g-1001884420011/m40003/full",
                                     "label": "site fence", "width": 1280, "height": 860}],
                         "caption": "Shot these on the walk over.",
                         "thumbUrl": "/media/c_g-1001884420011/m40003/thumb"})
        self._msg("c_u771019", "u771019", n - 7200_000, "text", text="Sending the FOIA response.")
        self._msg("c_u771019", ids.ME, n - 3000_000, "voice",
                  media={"url": "/media/c_u771019/m40005/voice", "duration": "0:34",
                         "wave": [3, 6, 12, 8, 15, 22, 14, 9, 18, 26, 20, 11, 7, 13, 19, 24]})
        self._msg(ids.SAVED, ids.ME, n - 5 * DAY, "text", text="Call Harriet Vance back.")
        self._msg("c_ch-1001884420099", ids.ME, n - DAY, "poll",
                  media={"question": "Lead art for the permits story",
                         "options": [{"text": "Crane detail", "votes": 4},
                                     {"text": "Notice board", "votes": 7}],
                         "totalVotes": 11, "voted": None})

    def _add_chat(self, cid: str, kind: str, title: str, tg: int, *, pinned: bool = False,
                  unread: int = 0, added: int | None = None, members: list[str] | None = None,
                  member_count: int | None = None, sign: bool | None = None,
                  user: str | None = None) -> None:
        chat: dict[str, Any] = {
            "id": cid, "tgChatId": str(tg), "type": kind, "title": title,
            "username": None, "avatarUrl": None,
            "initials": "★" if kind == "saved" else ids.initials(title),
            "enabled": True, "addedAt": added or _now(), "unreadCount": unread,
            "pinned": pinned, "muted": False, "archived": False, "draft": "",
        }
        if kind == "private":
            chat["userId"] = user
            chat["online"] = True
            chat["lastSeen"] = _now() - 600_000
        elif kind == "saved":
            chat["memberCount"] = 1
            chat["members"] = [ids.ME]
        else:
            chat["members"] = members or [ids.ME]
            chat["memberCount"] = member_count or len(members or [ids.ME])
            chat["admins"] = [ids.ME]
            chat["permissions"] = {"send": True, "media": True, "invite": True, "pin": False}
            chat["inviteLink"] = "t.me/+example"
            if sign is not None:
                chat["signMessages"] = sign
        self.chats[cid] = chat

    def _msg(self, chat_id: str, sender: str, date: int, mtype: str, **extra) -> dict:
        self.seq += 1
        user = self.users.get(sender, {"name": "Unknown"})
        m = {
            "id": f"m{self.seq}", "tgMessageId": str(self.seq), "chatId": chat_id,
            "senderId": sender, "senderName": user["name"],
            "senderColor": ids.sender_color(int(user.get("tgUserId", 1) or 1)),
            "outgoing": sender == ids.ME, "type": mtype, "text": extra.get("text", ""),
            "media": extra.get("media"), "link": extra.get("link"), "replyTo": extra.get("replyTo"),
            "forwardedFrom": extra.get("forwardedFrom"), "edited": False,
            "reactions": extra.get("reactions", {}),
            "status": extra.get("status", "read" if sender == ids.ME else "delivered"),
            "date": date, "pinned": extra.get("pinned", False), "deleted": False,
        }
        self.messages.append(m)
        return m

    # ----------------------------------------------------------------- infra
    async def start(self) -> None:
        HUB.set_status("connecting")
        await asyncio.sleep(0)
        HUB.set_status("online")

    async def stop(self) -> None:
        HUB.set_status("offline")

    def _chat(self, chat_id: str) -> dict:
        c = self.chats.get(chat_id)
        if not c or not c["enabled"]:
            raise UserFacing("That chat is not enabled. Run .addweb in it first.")
        return c

    def _shape(self, c: dict) -> dict:
        live = [m for m in self.messages if m["chatId"] == c["id"] and not m["deleted"]]
        out = dict(c)
        out["lastMessage"] = live[-1] if live else None
        return out

    def _find(self, chat_id: str, message_id: str) -> dict:
        for m in self.messages:
            if m["id"] == message_id and m["chatId"] == chat_id and not m["deleted"]:
                return m
        raise UserFacing("That message is no longer there.")

    # ----------------------------------------------------------------- reads
    async def bootstrap(self) -> dict:
        return {"me": dict(self.me), "contacts": await self.get_contacts(),
                "settings": await self.get_settings(), "savedChatId": ids.SAVED}

    async def get_chats(self) -> list[dict]:
        return [self._shape(c) for c in self.chats.values() if c["enabled"]]

    async def get_chat(self, chat_id: str) -> dict:
        return self._shape(self._chat(chat_id))

    async def get_messages(self, chat_id: str, before: int | None, limit: int) -> list[dict]:
        self._chat(chat_id)
        live = [m for m in self.messages if m["chatId"] == chat_id and not m["deleted"]]
        if before:
            live = [m for m in live if m["date"] < before]
        return [dict(m) for m in live[-limit:]]

    async def get_profile(self, user_id: str) -> dict:
        u = self.users.get(user_id)
        if not u:
            raise UserFacing("No such user.")
        return dict(u, blocked=user_id in self.blocked)

    async def get_shared_media(self, chat_id: str, kind: str | None):
        self._chat(chat_id)
        live = [m for m in self.messages if m["chatId"] == chat_id and not m["deleted"]]
        by = {
            "media": [{"label": (p.get("label") or "Photo"), "sub": "", "url": p.get("url")}
                      for m in live if m["type"] == "photo"
                      for p in (m["media"] or {}).get("photos", [])],
            "files": [{"label": (m["media"] or {}).get("fileName", "File"),
                       "sub": (m["media"] or {}).get("fileSize", ""),
                       "url": (m["media"] or {}).get("url")}
                      for m in live if m["type"] == "file"],
            "links": [{"label": m["link"]["title"], "sub": m["link"]["site"], "url": None}
                      for m in live if m.get("link")],
            "voice": [{"label": m["senderName"], "sub": (m["media"] or {}).get("duration", ""),
                       "url": (m["media"] or {}).get("url")}
                      for m in live if m["type"] == "voice"],
        }
        return by.get(kind, []) if kind else by

    async def search_all(self, query: str) -> dict:
        q = (query or "").strip().lower()
        if not q:
            return {"chats": [], "messages": [], "contacts": []}
        msgs = []
        for m in self.messages:
            if m["deleted"] or q not in (m["text"] or "").lower():
                continue
            msgs.append(dict(m, chatTitle=self.chats.get(m["chatId"], {}).get("title", "")))
        return {
            "chats": [self._shape(c) for c in self.chats.values()
                      if c["enabled"] and q in c["title"].lower()],
            "messages": msgs[:24],
            "contacts": [dict(u) for uid, u in self.users.items()
                         if uid != ids.ME and (q in u["name"].lower() or q in (u["username"] or "").lower())],
        }

    async def search_in_chat(self, chat_id: str, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        if not q:
            return []
        return [dict(m) for m in self.messages
                if m["chatId"] == chat_id and not m["deleted"] and q in (m["text"] or "").lower()]

    async def get_contacts(self) -> list[dict]:
        return [dict(u) for uid, u in self.users.items() if uid != ids.ME]

    async def get_settings(self) -> dict:
        return await settings_store.load()

    async def get_blocked(self) -> list[dict]:
        return [dict(self.users[u], blocked=True) for u in self.blocked if u in self.users]

    async def get_sessions(self) -> list[dict]:
        n = _now()
        return [
            {"id": "s1", "device": "Telethongram Web — this browser", "place": "Local",
             "current": True, "date": n},
            {"id": "s2", "device": "Telegram Desktop — macOS", "place": "Washington, DC",
             "current": False, "date": n - 2 * DAY},
        ]

    # ---------------------------------------------------------------- writes
    async def send_message(self, chat_id: str, payload: dict) -> dict:
        self._chat(chat_id)
        ptype = (payload or {}).get("type") or "text"
        med = (payload or {}).get("media") or None
        if ptype in ("photo", "file", "video", "gif") and med:
            url = (med.get("url") or ((med.get("photos") or [{}])[0].get("url")))
            handle = (url or "").rsplit("/", 1)[-1]
            row = await db.fetchone("SELECT rel_path FROM upload_handles WHERE handle=?", (handle,))
            if row is None or not row["rel_path"]:
                raise UserFacing(
                    "There was no file behind that attachment, so nothing was sent. "
                    "Pick a file from your computer and try again."
                )
        m = self._msg(chat_id, ids.ME, _now(), ptype, text=(payload or {}).get("text", ""),
                      media=med, replyTo=(payload or {}).get("replyTo"), status="sent")
        self.chats[chat_id]["draft"] = ""
        await db.execute("DELETE FROM drafts WHERE chat_id=?", (chat_id,))
        return dict(m)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> dict:
        m = self._find(chat_id, message_id)
        m["text"], m["edited"] = text, True
        HUB.emit("onMessageEdited", {"chatId": chat_id, "message": dict(m)})
        return dict(m)

    async def delete_message(self, chat_id: str, message_id: str, for_everyone: bool) -> dict:
        m = self._find(chat_id, message_id)
        m["deleted"] = True
        HUB.emit("onMessageDeleted", {"chatId": chat_id, "messageId": message_id,
                                      "forEveryone": bool(for_everyone)})
        return {"chatId": chat_id, "messageId": message_id}

    async def react(self, chat_id: str, message_id: str, emoji: str) -> dict:
        m = self._find(chat_id, message_id)
        arr = list(m["reactions"].get(emoji, []))
        if ids.ME in arr:
            arr.remove(ids.ME)
        else:
            arr.append(ids.ME)
        if arr:
            m["reactions"][emoji] = arr
        else:
            m["reactions"].pop(emoji, None)
        HUB.emit("onReaction", {"chatId": chat_id, "messageId": message_id,
                                "reactions": m["reactions"]})
        return dict(m)

    async def forward(self, from_chat: str, message_ids: list[str], to_chat: str) -> dict:
        self._chat(to_chat)
        count = 0
        for mid in message_ids:
            src = self._find(from_chat, mid)
            copy = self._msg(to_chat, ids.ME, _now(), src["type"], text=src["text"],
                             media=src["media"], forwardedFrom=src["senderName"], status="sent")
            HUB.emit("onNewMessage", {"chatId": to_chat, "message": dict(copy)})
            count += 1
        return {"count": count}

    async def vote_poll(self, chat_id: str, message_id: str, index: int) -> dict:
        m = self._find(chat_id, message_id)
        if m["type"] != "poll":
            raise UserFacing("That is not a poll.")
        p = m["media"]
        if p.get("voted") is not None:
            p["options"][p["voted"]]["votes"] -= 1
        else:
            p["totalVotes"] += 1
        p["options"][index]["votes"] += 1
        p["voted"] = index
        return dict(m)

    async def pin_message(self, chat_id: str, message_id: str) -> dict:
        for m in self.messages:
            if m["chatId"] == chat_id:
                m["pinned"] = False
        m = self._find(chat_id, message_id)
        m["pinned"] = True
        return dict(m)

    async def mark_read(self, chat_id: str) -> dict:
        self._chat(chat_id)["unreadCount"] = 0
        return {"chatId": chat_id}

    async def set_typing(self, chat_id: str) -> dict:
        return {"chatId": chat_id}

    async def save_draft(self, chat_id: str, text: str) -> dict:
        if chat_id in self.chats:
            self.chats[chat_id]["draft"] = text or ""
        await db.execute(
            "INSERT INTO drafts(chat_id, text, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (chat_id, text or "", db.now_ms()),
        )
        return {"chatId": chat_id}

    async def pin_chat(self, chat_id: str) -> dict:
        c = self._chat(chat_id)
        c["pinned"] = not c["pinned"]
        return self._shape(c)

    async def mute_chat(self, chat_id: str) -> dict:
        c = self._chat(chat_id)
        c["muted"] = not c["muted"]
        return self._shape(c)

    async def archive_chat(self, chat_id: str) -> dict:
        c = self._chat(chat_id)
        c["archived"] = not c["archived"]
        return self._shape(c)

    async def clear_history(self, chat_id: str) -> dict:
        self._chat(chat_id)
        for m in self.messages:
            if m["chatId"] == chat_id:
                m["deleted"] = True
        return {"chatId": chat_id}

    async def disable_chat(self, chat_id: str) -> dict:
        c = self._chat(chat_id)
        c["enabled"] = False           # never leaves the Telegram chat
        HUB.emit("onChatRemoved", {"chatId": chat_id})
        return {"chatId": chat_id}

    async def create_chat(self, kind: str, title: str, member_ids: list[str]) -> dict:
        tg = -1001884420000 - len(self.chats)
        cid = ids.chat_cid(tg, kind)
        self._add_chat(cid, kind, title or "Untitled", tg,
                       members=[ids.ME] + list(member_ids or []))
        chat = self._shape(self.chats[cid])
        HUB.emit("onChatAdded", {"chat": chat})
        return chat

    async def add_members(self, chat_id: str, ids_: list[str]) -> dict:
        c = self._chat(chat_id)
        c["members"] = list(dict.fromkeys((c.get("members") or []) + list(ids_ or [])))
        c["memberCount"] = len(c["members"])
        return self._shape(c)

    async def set_permission(self, chat_id: str, key: str, value: bool) -> dict:
        c = self._chat(chat_id)
        c.setdefault("permissions", {})[key] = bool(value)
        return self._shape(c)

    async def set_sign_messages(self, chat_id: str, value: bool) -> dict:
        c = self._chat(chat_id)
        c["signMessages"] = bool(value)
        return self._shape(c)

    async def block_user(self, user_id: str) -> dict:
        if user_id not in self.blocked:
            self.blocked.append(user_id)
        return {"userId": user_id, "blocked": True}

    async def unblock_user(self, user_id: str) -> dict:
        self.blocked = [b for b in self.blocked if b != user_id]
        return {"userId": user_id, "blocked": False}

    async def update_settings(self, payload: dict) -> dict:
        merged = await settings_store.save(settings_store.sanitize(payload))
        HUB.emit("settings", merged)
        return merged

    async def update_profile(self, payload: dict) -> dict:
        for k in ("name", "username", "bio"):
            if k in (payload or {}):
                self.me[k] = payload[k]
        self.me["initials"] = ids.initials(self.me["name"])
        return dict(self.me)

    async def terminate_session(self, session_id: str) -> dict:
        if session_id == "s1":
            raise UserFacing("That is the session this browser is using.")
        return {"id": session_id}

    # ----------------------------------------------------------------- media
    async def fetch_media(self, chat_id: str, message_id: str, variant: str):
        self._chat(chat_id)
        self._find(chat_id, message_id)
        # A 1x1 PNG stands in for real bytes so the proxy path is verifiable.
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
        )
        return png, "image/png"

    async def fetch_avatar(self, entity_id: str, variant: str):
        raise UserFacing("No profile photo.")

    # --------------------------------------------------------------- dev aid
    async def simulate_addweb(self, title: str = "Wire Watch") -> dict:
        tg = -1001884420077
        cid = ids.chat_cid(tg, "group")
        if cid in self.chats and self.chats[cid]["enabled"]:
            return await self.get_chat(cid)
        self._add_chat(cid, "group", title, tg, unread=1,
                       members=[ids.ME, "u771019"], added=_now())
        self._msg(cid, "u771019", _now(), "text", text="Wire copy will land here from now on.")
        chat = self._shape(self.chats[cid])
        HUB.emit("onChatAdded", {"chat": chat})
        return chat

    async def simulate_delweb(self) -> dict:
        cid = ids.chat_cid(-1001884420077, "group")
        if cid in self.chats:
            self.chats[cid]["enabled"] = False
        HUB.emit("onChatRemoved", {"chatId": cid})
        return {"chatId": cid}

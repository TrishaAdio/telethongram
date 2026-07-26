"""The real MTProto gateway: Telethon running as the user.

Everything the browser sees is built here from Telegram objects; no raw MTProto
object ever leaves this module. Every outbound call goes through the FLOOD_WAIT
scheduler.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient, events, functions, types, utils
from telethon.sessions import StringSession

from .. import db, ids, media, settings_store
from ..config import CFG
from ..crypto import unseal
from ..floodqueue import SCHEDULER, FloodWaitTooLong
from ..hub import HUB
from .base import Gateway, UserFacing

log = logging.getLogger("mtproto")

TYPING_TTL = 6.0


def _cmd_re() -> re.Pattern:
    p = re.escape(CFG.command_prefix)
    return re.compile(rf"^\s*{p}(addweb|delweb)\s*$", re.IGNORECASE)


class TelethonGateway(Gateway):
    name = "telethon"

    def __init__(self) -> None:
        self.client: Optional[TelegramClient] = None
        self.me: Any = None
        self.me_id: int = 0
        self._cmd = _cmd_re()
        self._dialog_cache: dict[int, Any] = {}
        self._dialog_cache_at = 0.0
        self._typing_timers: dict[tuple[str, str], asyncio.Task] = {}
        self._poll_options: dict[str, list[bytes]] = {}
        self._client_ids: dict[str, str] = {}   # tg key -> mo_ id, for echo dedup

    # ------------------------------------------------------------------ boot
    async def start(self) -> None:
        problems = CFG.problems()
        if problems:
            raise RuntimeError("; ".join(problems))
        session_string = unseal(CFG.session_path.read_bytes())
        self.client = TelegramClient(
            StringSession(session_string),
            CFG.api_id,
            CFG.api_hash,
            flood_sleep_threshold=0,       # we handle FLOOD_WAIT ourselves
            connection_retries=None,
            request_retries=1,
            auto_reconnect=True,
            catch_up=True,
        )
        del session_string
        HUB.set_status("connecting")
        await self.client.connect()
        if not await self.client.is_user_authorized():
            HUB.set_status("offline")
            raise RuntimeError("the stored Telegram session is no longer authorised")
        self.me = await self.client.get_me()
        self.me_id = int(self.me.id)
        await self._remember_entity(self.me)
        self._register_handlers()
        if CFG.auto_enable_saved:
            await self._ensure_enabled(self.me_id, "saved", via="bootstrap")
        await self.client.catch_up()
        HUB.set_status("online")
        log.info("MTProto client online as user %s", self.me_id)

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        HUB.set_status("offline")

    # ------------------------------------------------------------- internals
    async def _call(self, coro_factory, *, peer: str | None = None, lane: str = "high", label: str = ""):
        try:
            return await SCHEDULER.run(coro_factory, peer=peer, lane=lane, label=label)
        except FloodWaitTooLong as e:
            mins = max(1, round(e.seconds / 60))
            raise UserFacing(
                f"Telegram is rate-limiting this account for about {mins} more minute"
                f"{'s' if mins != 1 else ''}. Nothing was lost — try again after that."
            ) from None

    async def _remember_entity(self, entity: Any) -> str:
        tg_id = int(utils.get_peer_id(entity))
        kind = self._kind_of(entity)
        if kind == "private" or isinstance(entity, types.User):
            cid = ids.user_cid(tg_id, self.me_id)
        else:
            cid = ids.chat_cid(tg_id, kind, self.me_id)
        title = utils.get_display_name(entity) or ""
        username = getattr(entity, "username", None)
        photo = getattr(entity, "photo", None)
        photo_id = str(getattr(photo, "photo_id", "") or getattr(photo, "stripped_thumb", b"").hex()[:16]) or None
        await db.execute(
            "INSERT INTO entities(client_id, tg_id, kind, title, username, photo_id, seen_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET"
            " title=excluded.title, username=excluded.username, photo_id=excluded.photo_id,"
            " seen_at=excluded.seen_at",
            (cid, tg_id, kind, title, f"@{username}" if username else None, photo_id, db.now_ms()),
        )
        return cid

    @staticmethod
    def _kind_of(entity: Any) -> str:
        if isinstance(entity, types.User):
            return "private"
        if isinstance(entity, types.Chat) or isinstance(entity, types.ChatForbidden):
            return "group"
        if isinstance(entity, types.Channel):
            return "group" if getattr(entity, "megagroup", False) else "channel"
        return "group"

    async def _tg_id(self, client_id: str) -> int:
        if client_id == ids.SAVED:
            return self.me_id
        if client_id == ids.ME:
            return self.me_id
        row = await db.fetchone("SELECT tg_id FROM entities WHERE client_id=?", (client_id,))
        if row:
            return int(row["tg_id"])
        m = re.fullmatch(r"c_(?:u|g|ch)(-?\d+)|u(-?\d+)", client_id or "")
        if m:
            return ids.parse_int64(m.group(1) or m.group(2))
        raise UserFacing("That chat is not enabled. Run .addweb in it first.")

    async def _entity(self, client_id: str):
        tg_id = await self._tg_id(client_id)
        try:
            return await self.client.get_input_entity(tg_id)
        except (ValueError, TypeError):
            try:
                return await self.client.get_entity(tg_id)
            except Exception:
                raise UserFacing(
                    "Telegram would not resolve that chat. Open it once in your Telegram app, "
                    "then reload."
                ) from None

    async def _enabled_row(self, client_id: str):
        row = await db.fetchone(
            "SELECT * FROM enabled_chats WHERE client_id=? AND enabled=1", (client_id,)
        )
        if not row:
            raise UserFacing("That chat is not enabled. Run .addweb in it first.")
        return row

    async def _dialogs(self, force: bool = False) -> dict[int, Any]:
        if force or (time.monotonic() - self._dialog_cache_at) > 20:
            dialogs = await self._call(
                lambda: self.client.get_dialogs(limit=200, archived=None),
                lane="low", label="get_dialogs",
            )
            cache = {}
            for d in dialogs:
                try:
                    cache[int(utils.get_peer_id(d.entity))] = d
                    await self._remember_entity(d.entity)
                except Exception:
                    continue
            self._dialog_cache, self._dialog_cache_at = cache, time.monotonic()
        return self._dialog_cache

    async def _is_filtered(self, tg_chat_id: int, tg_msg_id: int) -> bool:
        row = await db.fetchone(
            "SELECT 1 FROM filtered_msgs WHERE tg_chat_id=? AND tg_msg_id=?",
            (tg_chat_id, tg_msg_id),
        )
        return row is not None

    # ------------------------------------------------------------- bootstrap
    async def bootstrap(self) -> dict[str, Any]:
        return {
            "me": await self._user_shape(self.me),
            "contacts": await self.get_contacts(),
            "settings": await self.get_settings(),
            "savedChatId": ids.SAVED,
        }

    async def _user_shape(self, user: Any, *, blocked: bool = False) -> dict:
        name = utils.get_display_name(user) or "Unknown"
        uid = int(user.id)
        photo_id = getattr(getattr(user, "photo", None), "photo_id", None)
        cid = ids.user_cid(uid, self.me_id)
        return {
            "id": cid,
            "tgUserId": ids.s64(uid),
            "name": name,
            "username": f"@{user.username}" if getattr(user, "username", None) else None,
            "phone": f"+{user.phone}" if getattr(user, "phone", None) else None,
            "bio": getattr(user, "about", None) or "",
            "initials": ids.initials(name),
            "avatarUrl": f"/media/avatar/{cid}/{photo_id}/thumb" if photo_id else None,
            "online": isinstance(getattr(user, "status", None), types.UserStatusOnline),
            "lastSeen": self._last_seen(getattr(user, "status", None)),
            "blocked": blocked,
        }

    @staticmethod
    def _last_seen(status: Any) -> Optional[int]:
        if isinstance(status, types.UserStatusOffline) and status.was_online:
            return int(status.was_online.timestamp() * 1000)
        return None

    # ----------------------------------------------------------------- chats
    async def get_chats(self) -> list[dict]:
        rows = await db.fetchall("SELECT * FROM enabled_chats WHERE enabled=1")
        if not rows:
            return []
        dialogs = await self._dialogs()
        out = []
        for row in rows:
            try:
                out.append(await self._chat_shape(row, dialogs.get(int(row["tg_chat_id"]))))
            except Exception:
                log.exception("could not shape chat %s", row["client_id"])
        return out

    async def get_chat(self, chat_id: str) -> dict:
        row = await self._enabled_row(chat_id)
        dialogs = await self._dialogs()
        return await self._chat_shape(row, dialogs.get(int(row["tg_chat_id"])), full=True)

    async def _chat_shape(self, row, dialog: Any, full: bool = False) -> dict:
        cid = row["client_id"]
        kind = row["kind"]
        tg_id = int(row["tg_chat_id"])
        entity = getattr(dialog, "entity", None)
        if entity is None:
            try:
                entity = await self.client.get_entity(tg_id)
                await self._remember_entity(entity)
            except Exception:
                entity = None

        title = "Saved Messages" if kind == "saved" else (utils.get_display_name(entity) or "Chat")
        username = getattr(entity, "username", None)
        photo_id = getattr(getattr(entity, "photo", None), "photo_id", None)
        draft_row = await db.fetchone("SELECT text FROM drafts WHERE chat_id=?", (cid,))

        chat: dict[str, Any] = {
            "id": cid,
            "tgChatId": ids.s64(tg_id),
            "type": kind,
            "title": title,
            "username": f"@{username}" if username else None,
            "avatarUrl": f"/media/avatar/{cid}/{photo_id}/thumb" if photo_id else None,
            "initials": "★" if kind == "saved" else ids.initials(title),
            "enabled": True,
            "addedAt": int(row["added_at"]),
            "unreadCount": int(getattr(dialog, "unread_count", 0) or 0),
            "pinned": bool(getattr(dialog, "pinned", False)),
            "muted": self._is_muted(dialog),
            "archived": bool(getattr(dialog, "archived", False)),
            "lastMessage": None,
            "draft": draft_row["text"] if draft_row else "",
        }

        last = getattr(dialog, "message", None)
        if last is not None and not await self._is_filtered(tg_id, int(last.id)):
            if int(getattr(last, "date", None).timestamp() * 1000) >= int(row["hide_before"]):
                chat["lastMessage"] = await self._message_shape(last, cid)

        if kind == "private":
            chat["userId"] = ids.user_cid(tg_id, self.me_id)
            chat["online"] = isinstance(getattr(entity, "status", None), types.UserStatusOnline)
            chat["lastSeen"] = self._last_seen(getattr(entity, "status", None))
        elif kind == "saved":
            chat["memberCount"] = 1
            chat["members"] = [ids.ME]
        else:
            chat["memberCount"] = int(getattr(entity, "participants_count", 0) or 0)
            if kind == "channel":
                chat["signMessages"] = bool(getattr(entity, "signatures", False))
            if getattr(entity, "username", None):
                chat["inviteLink"] = f"t.me/{entity.username}"
            if full:
                await self._augment_full(chat, entity, kind)
        return chat

    @staticmethod
    def _is_muted(dialog: Any) -> bool:
        raw = getattr(dialog, "dialog", None)
        ns = getattr(raw, "notify_settings", None)
        mute_until = getattr(ns, "mute_until", None)
        if mute_until is None:
            return False
        if isinstance(mute_until, int):
            return mute_until > time.time()
        try:
            return mute_until.timestamp() > time.time()
        except Exception:
            return False

    async def _augment_full(self, chat: dict, entity: Any, kind: str) -> None:
        """Members, admins, permissions - only for the info panel."""
        if entity is None or kind == "channel":
            return
        try:
            participants = await self._call(
                lambda: self.client.get_participants(entity, limit=200),
                lane="low", label="get_participants",
            )
        except (UserFacing, Exception):
            return
        members, admins = [], []
        for p in participants:
            cid = ids.user_cid(int(p.id), self.me_id)
            members.append(cid)
            part = getattr(p, "participant", None)
            if isinstance(part, (types.ChatParticipantAdmin, types.ChatParticipantCreator,
                                 types.ChannelParticipantAdmin, types.ChannelParticipantCreator)):
                admins.append(cid)
            await self._remember_entity(p)
        chat["members"] = members
        chat["admins"] = admins
        chat["memberCount"] = chat.get("memberCount") or len(members)
        rights = getattr(entity, "default_banned_rights", None)
        chat["permissions"] = {
            "send": not getattr(rights, "send_messages", False),
            "media": not getattr(rights, "send_media", False),
            "invite": not getattr(rights, "invite_users", False),
            "pin": not getattr(rights, "pin_messages", False),
        }
        if not chat.get("inviteLink"):
            chat["inviteLink"] = None

    # -------------------------------------------------------------- messages
    async def get_messages(self, chat_id: str, before: int | None, limit: int) -> list[dict]:
        row = await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        kwargs: dict[str, Any] = {"limit": max(1, min(100, limit))}
        if before:
            kwargs["offset_date"] = before / 1000.0
        msgs = await self._call(
            lambda: self.client.get_messages(entity, **kwargs), lane="low", label="get_messages"
        )
        tg_id = int(row["tg_chat_id"])
        hide_before = int(row["hide_before"])
        out = []
        for m in reversed(list(msgs)):
            if isinstance(m, types.MessageEmpty):
                continue
            if await self._is_filtered(tg_id, int(m.id)):
                continue
            date_ms = int(m.date.timestamp() * 1000)
            if date_ms < hide_before:
                continue
            out.append(await self._message_shape(m, chat_id))
        return out

    async def _message_shape(self, m: Any, chat_id: str) -> dict:
        sender_id = int(getattr(m, "sender_id", 0) or 0)
        sender = await self._sender_of(m)
        name = utils.get_display_name(sender) or ("You" if m.out else "Unknown")
        mtype, media_obj = await self._media_shape(m, chat_id)
        reply_to = None
        if getattr(m, "reply_to", None) and getattr(m.reply_to, "reply_to_msg_id", None):
            reply_to = ids.msg_cid(m.reply_to.reply_to_msg_id)
        fwd = None
        if getattr(m, "fwd_from", None):
            fwd = getattr(m.fwd_from, "from_name", None)
            if not fwd and getattr(m.fwd_from, "from_id", None):
                try:
                    fwd = utils.get_display_name(await self.client.get_entity(m.fwd_from.from_id))
                except Exception:
                    fwd = "Forwarded"
        return {
            "id": ids.msg_cid(int(m.id)),
            "tgMessageId": ids.s64(int(m.id)),
            "chatId": chat_id,
            "senderId": ids.user_cid(sender_id, self.me_id) if sender_id else ids.ME,
            "senderName": name,
            "senderColor": ids.sender_color(sender_id or self.me_id),
            "outgoing": bool(m.out),
            "type": mtype,
            "text": "" if mtype in ("photo", "video", "voice", "file") else (m.message or ""),
            "media": media_obj,
            "link": self._link_preview(m),
            "replyTo": reply_to,
            "forwardedFrom": fwd,
            "edited": bool(getattr(m, "edit_date", None)),
            "reactions": await self._reactions_shape(m),
            "status": self._status_of(m),
            "date": int(m.date.timestamp() * 1000),
            "pinned": bool(getattr(m, "pinned", False)),
        }

    async def _sender_of(self, m: Any):
        try:
            s = await m.get_sender()
            if s is not None:
                await self._remember_entity(s)
            return s
        except Exception:
            return None

    @staticmethod
    def _status_of(m: Any) -> str:
        if not m.out:
            return "delivered"
        return "read" if not getattr(m, "media_unread", False) else "sent"

    def _link_preview(self, m: Any) -> Optional[dict]:
        wp = getattr(m, "media", None)
        if isinstance(wp, types.MessageMediaWebPage) and isinstance(wp.webpage, types.WebPage):
            page = wp.webpage
            return {
                "site": page.site_name or (page.display_url or "").split("/")[0],
                "title": page.title or "",
                "desc": page.description or "",
            }
        return None

    async def _reactions_shape(self, m: Any) -> dict:
        r = getattr(m, "reactions", None)
        if not r:
            return {}
        out: dict[str, list[str]] = {}
        recent = {}
        for rr in (getattr(r, "recent_reactions", None) or []):
            emo = getattr(rr.reaction, "emoticon", None)
            if not emo:
                continue
            try:
                recent.setdefault(emo, []).append(
                    ids.user_cid(int(utils.get_peer_id(rr.peer_id)), self.me_id)
                )
            except Exception:
                continue
        for res in (getattr(r, "results", None) or []):
            emo = getattr(res.reaction, "emoticon", None)
            if not emo:
                continue
            known = list(dict.fromkeys(recent.get(emo, [])))
            if getattr(res, "chosen_order", None) is not None and ids.ME not in known:
                known.insert(0, ids.ME)
            # Telegram only gives counts for channels; pad so the count is right.
            while len(known) < int(res.count or 0):
                known.append(f"u_other{len(known)}")
            out[emo] = known[: int(res.count or 0)] or known
        return out

    async def _media_shape(self, m: Any, chat_id: str) -> tuple[str, Optional[dict]]:
        base = f"/media/{chat_id}/{ids.msg_cid(int(m.id))}"
        med = getattr(m, "media", None)
        caption = m.message or ""
        if med is None or isinstance(med, types.MessageMediaWebPage):
            return "text", None
        if isinstance(med, types.MessageMediaPhoto):
            photo = med.photo
            w = h = 0
            for size in (getattr(photo, "sizes", None) or []):
                w = max(w, getattr(size, "w", 0) or 0)
                h = max(h, getattr(size, "h", 0) or 0)
            return "photo", {
                "photos": [{"url": f"{base}/full", "label": caption or "Photo", "width": w, "height": h}],
                "caption": caption,
                "thumbUrl": f"{base}/thumb",
            }
        if isinstance(med, types.MessageMediaGeo) or isinstance(med, types.MessageMediaGeoLive):
            geo = med.geo
            return "location", {
                "lat": getattr(geo, "lat", 0),
                "lon": getattr(geo, "long", 0),
                "place": "Shared location",
                "sub": "Live location" if isinstance(med, types.MessageMediaGeoLive) else "",
            }
        if isinstance(med, types.MessageMediaVenue):
            return "location", {
                "lat": med.geo.lat, "lon": med.geo.long, "place": med.title, "sub": med.address,
            }
        if isinstance(med, types.MessageMediaContact):
            name = f"{med.first_name} {med.last_name}".strip()
            return "contact", {
                "name": name,
                "phone": f"+{med.phone_number}",
                "note": "",
                "userId": ids.user_cid(int(med.user_id), self.me_id) if med.user_id else None,
            }
        if isinstance(med, types.MessageMediaPoll):
            poll, results = med.poll, med.results
            options, voted = [], None
            counts = {bytes(v.option): v.voters for v in (results.results or [])}
            chosen = [bytes(v.option) for v in (results.results or []) if getattr(v, "chosen", False)]
            opt_bytes = []
            for i, ans in enumerate(poll.answers):
                ob = bytes(ans.option)
                opt_bytes.append(ob)
                options.append({"text": self._poll_text(ans.text), "votes": int(counts.get(ob, 0) or 0)})
                if ob in chosen:
                    voted = i
            self._poll_options[f"{chat_id}:{m.id}"] = opt_bytes
            return "poll", {
                "question": self._poll_text(poll.question),
                "options": options,
                "totalVotes": int(getattr(results, "total_voters", 0) or 0),
                "voted": voted,
            }
        if isinstance(med, types.MessageMediaDocument):
            doc = med.document
            attrs = {type(a).__name__: a for a in (getattr(doc, "attributes", None) or [])}
            mime = getattr(doc, "mime_type", "") or ""
            size = int(getattr(doc, "size", 0) or 0)
            audio = attrs.get("DocumentAttributeAudio")
            video = attrs.get("DocumentAttributeVideo")
            sticker = attrs.get("DocumentAttributeSticker")
            filename = getattr(attrs.get("DocumentAttributeFilename"), "file_name", None)

            if sticker is not None:
                return "sticker", {"emoji": getattr(sticker, "alt", "") or "🙂", "url": f"{base}/full"}
            if audio is not None and getattr(audio, "voice", False):
                return "voice", {
                    "url": f"{base}/voice",
                    "duration": media.human_duration(getattr(audio, "duration", 0)),
                    "wave": media.decode_waveform(getattr(audio, "waveform", b"") or b""),
                }
            if getattr(med, "video", False) or (video is not None and mime.startswith("video")):
                if attrs.get("DocumentAttributeAnimated") is not None:
                    return "gif", {"url": f"{base}/full", "label": filename or "GIF"}
                return "video", {
                    "url": f"{base}/full",
                    "thumbUrl": f"{base}/thumb",
                    "label": filename or caption or "Video",
                    "duration": media.human_duration(getattr(video, "duration", 0)),
                    "caption": caption,
                }
            return "file", {
                "url": f"{base}/file",
                "fileName": filename or "file",
                "fileSize": media.human_size(size),
                "fileKind": (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else "file",
                "mime": mime,
                "caption": caption,
            }
        return "text", None

    @staticmethod
    def _poll_text(value: Any) -> str:
        # Layer 158+ wraps poll text in TextWithEntities.
        return getattr(value, "text", value) if not isinstance(value, str) else value

    # ------------------------------------------------------------- profiles
    async def get_profile(self, user_id: str) -> dict:
        tg = await self._tg_id(user_id)
        try:
            entity = await self._entity(user_id)
            full = await self._call(
                lambda: self.client(functions.users.GetFullUserRequest(entity)),
                lane="low", label="GetFullUser",
            )
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("Telegram would not return that profile.") from None
        user = next((u for u in full.users if int(u.id) == abs(int(tg))), None)
        shape = await self._user_shape(user or self.me)
        shape["bio"] = getattr(full.full_user, "about", None) or ""
        shape["blocked"] = bool(getattr(full.full_user, "blocked", False))
        return shape

    async def get_contacts(self) -> list[dict]:
        res = await self._call(
            lambda: self.client(functions.contacts.GetContactsRequest(hash=0)),
            lane="low", label="GetContacts",
        )
        out = []
        for u in getattr(res, "users", []) or []:
            if int(u.id) == self.me_id:
                continue
            await self._remember_entity(u)
            out.append(await self._user_shape(u))
        return out

    async def get_blocked(self) -> list[dict]:
        res = await self._call(
            lambda: self.client(functions.contacts.GetBlockedRequest(offset=0, limit=100)),
            lane="low", label="GetBlocked",
        )
        out = []
        for u in getattr(res, "users", []) or []:
            out.append(await self._user_shape(u, blocked=True))
        return out

    async def get_sessions(self) -> list[dict]:
        res = await self._call(
            lambda: self.client(functions.account.GetAuthorizationsRequest()),
            lane="low", label="GetAuthorizations",
        )
        out = []
        for a in res.authorizations:
            out.append({
                "id": str(a.hash),
                "device": f"{a.app_name} — {a.device_model}, {a.platform}".strip(" —"),
                "place": ", ".join(x for x in (a.country, a.region) if x) or "Unknown",
                "current": bool(a.current),
                "date": int(a.date_active.timestamp() * 1000),
            })
        return out

    async def terminate_session(self, session_id: str) -> dict:
        if session_id in ("0", ""):
            raise UserFacing("That is this bridge's own Telegram session — it cannot be ended here.")
        try:
            h = int(session_id)
        except ValueError:
            raise UserFacing("That session id was not recognised.") from None
        current = [s for s in await self.get_sessions() if s["current"]]
        if current and current[0]["id"] == session_id:
            raise UserFacing(
                "That is the session Telethongram itself uses. Ending it would disconnect the bridge."
            )
        await self._call(
            lambda: self.client(functions.account.ResetAuthorizationRequest(hash=h)),
            label="ResetAuthorization",
        )
        await db.audit("terminate_session", f"hash={session_id}")
        return {"id": session_id}

    # ------------------------------------------------------------- searching
    async def search_all(self, query: str) -> dict:
        q = (query or "").strip()
        if not q:
            return {"chats": [], "messages": [], "contacts": []}
        chats = [c for c in await self.get_chats() if q.lower() in (c["title"] or "").lower()]
        enabled = {c["id"]: c["title"] for c in await self.get_chats()}
        messages = []
        try:
            found = await self._call(
                lambda: self.client.get_messages(None, search=q, limit=40),
                lane="low", label="SearchGlobal",
            )
            for m in found:
                try:
                    tg_chat = int(utils.get_peer_id(m.peer_id))
                except Exception:
                    continue
                row = await db.fetchone(
                    "SELECT client_id FROM enabled_chats WHERE tg_chat_id=? AND enabled=1", (tg_chat,)
                )
                if not row:
                    continue  # never leak messages from chats that are not enabled
                if await self._is_filtered(tg_chat, int(m.id)):
                    continue
                shape = await self._message_shape(m, row["client_id"])
                shape["chatTitle"] = enabled.get(row["client_id"], "")
                messages.append(shape)
                if len(messages) >= 24:
                    break
        except UserFacing:
            raise
        except Exception:
            log.exception("global search failed")
        contacts = [c for c in await self.get_contacts()
                    if q.lower() in c["name"].lower() or q.lower() in (c["username"] or "").lower()]
        return {"chats": chats, "messages": messages, "contacts": contacts}

    async def search_in_chat(self, chat_id: str, query: str) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        row = await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        found = await self._call(
            lambda: self.client.get_messages(entity, search=q, limit=60),
            lane="low", label="search_in_chat",
        )
        out = []
        for m in reversed(list(found)):
            if await self._is_filtered(int(row["tg_chat_id"]), int(m.id)):
                continue
            out.append(await self._message_shape(m, chat_id))
        return out

    async def get_shared_media(self, chat_id: str, kind: str | None):
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        wanted = [kind] if kind else ["media", "files", "links", "voice"]
        filters = {
            "media": types.InputMessagesFilterPhotoVideo(),
            "files": types.InputMessagesFilterDocument(),
            "links": types.InputMessagesFilterUrl(),
            "voice": types.InputMessagesFilterVoice(),
        }
        result: dict[str, list[dict]] = {}
        for w in wanted:
            flt = filters.get(w)
            if flt is None:
                result[w] = []
                continue
            msgs = await self._call(
                lambda flt=flt: self.client.get_messages(entity, limit=48, filter=flt),
                lane="low", label=f"shared:{w}",
            )
            items = []
            for m in msgs:
                mtype, med = await self._media_shape(m, chat_id)
                if w == "links":
                    link = self._link_preview(m)
                    items.append({"label": (link or {}).get("title") or (m.message or "")[:60],
                                  "sub": (link or {}).get("site") or "", "url": None})
                elif w == "files" and med:
                    items.append({"label": med.get("fileName", "File"),
                                  "sub": med.get("fileSize", ""), "url": med.get("url")})
                elif w == "voice" and med:
                    sender = await self._sender_of(m)
                    items.append({"label": utils.get_display_name(sender) or "Voice",
                                  "sub": med.get("duration", ""), "url": med.get("url")})
                elif w == "media" and med:
                    if mtype == "photo":
                        for p in med.get("photos", []):
                            items.append({"label": p.get("label") or "Photo", "sub": "", "url": p.get("url")})
                    else:
                        items.append({"label": med.get("label") or mtype.title(),
                                      "sub": med.get("duration", ""), "url": med.get("url")})
            result[w] = items
        return result[kind] if kind else result

    # ---------------------------------------------------------------- writes
    async def send_message(self, chat_id: str, payload: dict) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        ptype = (payload or {}).get("type") or "text"
        text = (payload or {}).get("text") or ""
        med = (payload or {}).get("media") or {}
        reply_to = await self._resolve_msg_id(chat_id, payload.get("replyTo")) if payload.get("replyTo") else None
        peer = chat_id

        if ptype == "text":
            if not text.strip():
                raise UserFacing("Nothing to send.")
            msg = await self._call(
                lambda: self.client.send_message(entity, text, reply_to=reply_to),
                peer=peer, label="send_message",
            )
        elif ptype == "poll":
            msg = await self._send_poll(entity, med, reply_to, peer)
        elif ptype == "contact":
            first, _, last = (med.get("name") or "Contact").partition(" ")
            msg = await self._call(
                lambda: self.client.send_file(
                    entity,
                    types.InputMediaContact(
                        phone_number=(med.get("phone") or "").replace(" ", ""),
                        first_name=first, last_name=last, vcard="",
                    ),
                    reply_to=reply_to,
                ),
                peer=peer, label="send_contact",
            )
        elif ptype == "location":
            lat, lon = med.get("lat"), med.get("lon")
            if lat is None or lon is None:
                raise UserFacing(
                    "That location had no coordinates attached, so Telegram could not accept it."
                )
            msg = await self._call(
                lambda: self.client.send_file(
                    entity,
                    types.InputMediaGeoPoint(types.InputGeoPoint(lat=float(lat), long=float(lon))),
                    reply_to=reply_to,
                ),
                peer=peer, label="send_geo",
            )
        elif ptype == "sticker":
            msg = await self._send_sticker(entity, med, reply_to, peer)
        elif ptype in ("photo", "video", "file", "gif", "voice"):
            msg = await self._send_upload(entity, ptype, med, reply_to, peer)
        else:
            raise UserFacing(f"Telethongram cannot send a “{ptype}” message.")

        await db.execute("DELETE FROM drafts WHERE chat_id=?", (chat_id,))
        shape = await self._message_shape(msg, chat_id)
        shape["status"] = "sent"
        return shape

    async def _send_poll(self, entity, med: dict, reply_to, peer: str):
        question = (med.get("question") or "Poll")[:255]
        answers = [(o.get("text") or f"Option {i+1}")[:100]
                   for i, o in enumerate(med.get("options") or [])][:10]
        if len(answers) < 2:
            raise UserFacing("A poll needs at least two options.")

        def build_poll():
            try:
                q = types.TextWithEntities(text=question, entities=[])
                ans = [types.PollAnswer(text=types.TextWithEntities(text=a, entities=[]),
                                        option=bytes([i])) for i, a in enumerate(answers)]
            except (AttributeError, TypeError):
                q = question
                ans = [types.PollAnswer(text=a, option=bytes([i])) for i, a in enumerate(answers)]
            return types.InputMediaPoll(poll=types.Poll(id=0, question=q, answers=ans))

        try:
            return await self._call(
                lambda: self.client.send_file(entity, build_poll(), reply_to=reply_to),
                peer=peer, label="send_poll",
            )
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("Telegram rejected that poll.") from None

    async def _send_sticker(self, entity, med: dict, reply_to, peer: str):
        emoji = med.get("emoji") or "🙂"
        try:
            res = await self._call(
                lambda: self.client(functions.messages.GetStickersRequest(emoticon=emoji, hash=0)),
                lane="low", label="GetStickers",
            )
            docs = getattr(res, "stickers", None) or []
            if docs:
                return await self._call(
                    lambda: self.client.send_file(entity, docs[0], reply_to=reply_to),
                    peer=peer, label="send_sticker",
                )
        except UserFacing:
            raise
        except Exception:
            log.info("no sticker matched %s; sending the emoji as text", emoji)
        return await self._call(
            lambda: self.client.send_message(entity, emoji, reply_to=reply_to),
            peer=peer, label="send_emoji",
        )

    async def _send_upload(self, entity, ptype: str, med: dict, reply_to, peer: str):
        url = med.get("url") or ((med.get("photos") or [{}])[0].get("url"))
        handle = (url or "").rsplit("/", 1)[-1] if url else ""
        row = await db.fetchone("SELECT * FROM upload_handles WHERE handle=?", (handle,)) if handle else None
        if row is None or not row["rel_path"]:
            raise UserFacing(
                "There was no file behind that attachment, so nothing was sent. "
                "Pick a file from your computer and try again."
            )
        path = CFG.upload_dir / row["rel_path"]
        if not path.exists():
            raise UserFacing("That upload expired before it could be sent. Try attaching it again.")
        caption = med.get("caption") or ""
        force_doc = ptype == "file"
        voice_note = ptype == "voice"
        msg = await self._call(
            lambda: self.client.send_file(
                entity, str(path), caption=caption, reply_to=reply_to,
                force_document=force_doc, voice_note=voice_note,
                attributes=None, file_name=row["name"],
            ),
            peer=peer, label=f"send_{ptype}",
        )
        await db.execute("UPDATE upload_handles SET consumed=1 WHERE handle=?", (handle,))
        return msg

    async def _resolve_msg_id(self, chat_id: str, message_id: str) -> int:
        if not message_id:
            raise UserFacing("No message was selected.")
        if ids.is_client_msg_id(message_id):
            row = await db.fetchone(
                "SELECT tg_msg_id FROM msg_ids WHERE client_id=? AND chat_id=?", (message_id, chat_id)
            )
            if row and row["tg_msg_id"]:
                return int(row["tg_msg_id"])
            raise UserFacing("That message is still being sent. Try again in a moment.")
        m = re.fullmatch(r"m(\d+)", message_id or "")
        if not m:
            raise UserFacing("That message id was not recognised.")
        return int(m.group(1))

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        try:
            msg = await self._call(
                lambda: self.client.edit_message(entity, mid, text), peer=chat_id, label="edit"
            )
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("Telegram would not edit that message (it may be too old).") from None
        return await self._message_shape(msg, chat_id)

    async def delete_message(self, chat_id: str, message_id: str, for_everyone: bool) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        await self._call(
            lambda: self.client.delete_messages(entity, [mid], revoke=bool(for_everyone)),
            peer=chat_id, label="delete",
        )
        return {"chatId": chat_id, "messageId": message_id}

    async def react(self, chat_id: str, message_id: str, emoji: str) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        current = await self._call(
            lambda: self.client.get_messages(entity, ids=mid), lane="low", label="get_one"
        )
        mine = []
        if current and getattr(current, "reactions", None):
            for res in (current.reactions.results or []):
                if getattr(res, "chosen_order", None) is not None:
                    emo = getattr(res.reaction, "emoticon", None)
                    if emo:
                        mine.append(emo)
        target = [] if emoji in mine else [types.ReactionEmoji(emoticon=emoji)]
        try:
            await self._call(
                lambda: self.client(functions.messages.SendReactionRequest(
                    peer=entity, msg_id=mid, reaction=target)),
                peer=chat_id, label="react",
            )
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("Telegram would not accept that reaction here.") from None
        fresh = await self._call(
            lambda: self.client.get_messages(entity, ids=mid), lane="low", label="get_one"
        )
        return await self._message_shape(fresh, chat_id)

    async def forward(self, from_chat: str, message_ids: list[str], to_chat: str) -> dict:
        await self._enabled_row(from_chat)
        src = await self._entity(from_chat)
        dst = await self._entity(to_chat)
        mids = [await self._resolve_msg_id(from_chat, m) for m in message_ids]
        if not mids:
            raise UserFacing("Nothing was selected to forward.")
        await self._call(
            lambda: self.client.forward_messages(dst, mids, src), peer=to_chat, label="forward"
        )
        return {"count": len(mids)}

    async def vote_poll(self, chat_id: str, message_id: str, index: int) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        options = self._poll_options.get(f"{chat_id}:{mid}")
        if not options:
            await self.get_messages(chat_id, None, 40)
            options = self._poll_options.get(f"{chat_id}:{mid}")
        if not options or index >= len(options):
            raise UserFacing("That poll option is no longer available.")
        await self._call(
            lambda: self.client(functions.messages.SendVoteRequest(
                peer=entity, msg_id=mid, options=[options[index]])),
            peer=chat_id, label="vote",
        )
        fresh = await self._call(
            lambda: self.client.get_messages(entity, ids=mid), lane="low", label="get_one"
        )
        return await self._message_shape(fresh, chat_id)

    async def pin_message(self, chat_id: str, message_id: str) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        await self._call(
            lambda: self.client.pin_message(entity, mid, notify=False), peer=chat_id, label="pin"
        )
        fresh = await self._call(
            lambda: self.client.get_messages(entity, ids=mid), lane="low", label="get_one"
        )
        return await self._message_shape(fresh, chat_id)

    async def mark_read(self, chat_id: str) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        await self._call(
            lambda: self.client.send_read_acknowledge(entity), peer=chat_id, lane="low", label="read"
        )
        self._dialog_cache_at = 0.0
        return {"chatId": chat_id}

    async def set_typing(self, chat_id: str) -> dict:
        entity = await self._entity(chat_id)
        try:
            await self.client(functions.messages.SetTypingRequest(
                peer=entity, action=types.SendMessageTypingAction()))
        except Exception:
            pass  # fire and forget, never surfaced
        return {"chatId": chat_id}

    async def save_draft(self, chat_id: str, text: str) -> dict:
        await db.execute(
            "INSERT INTO drafts(chat_id, text, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (chat_id, text or "", db.now_ms()),
        )
        if CFG.telegram_drafts:
            try:
                entity = await self._entity(chat_id)
                await self.client(functions.messages.SaveDraftRequest(peer=entity, message=text or ""))
            except Exception:
                log.debug("could not mirror the draft to Telegram")
        return {"chatId": chat_id}

    async def pin_chat(self, chat_id: str) -> dict:
        row = await self._enabled_row(chat_id)
        dialogs = await self._dialogs()
        current = bool(getattr(dialogs.get(int(row["tg_chat_id"])), "pinned", False))
        if CFG.mirror_dialog_state:
            entity = await self._entity(chat_id)
            await self._call(
                lambda: self.client(functions.messages.ToggleDialogPinRequest(
                    peer=entity, pinned=not current)),
                peer=chat_id, label="pin_chat",
            )
        await self._dialogs(force=True)
        return await self.get_chat(chat_id)

    async def mute_chat(self, chat_id: str) -> dict:
        row = await self._enabled_row(chat_id)
        dialogs = await self._dialogs()
        muted = self._is_muted(dialogs.get(int(row["tg_chat_id"])))
        if CFG.mirror_dialog_state:
            entity = await self._entity(chat_id)
            until = 0 if muted else 2 ** 31 - 1
            await self._call(
                lambda: self.client(functions.account.UpdateNotifySettingsRequest(
                    peer=types.InputNotifyPeer(entity),
                    settings=types.InputPeerNotifySettings(mute_until=until))),
                peer=chat_id, label="mute",
            )
        await self._dialogs(force=True)
        return await self.get_chat(chat_id)

    async def archive_chat(self, chat_id: str) -> dict:
        row = await self._enabled_row(chat_id)
        dialogs = await self._dialogs()
        archived = bool(getattr(dialogs.get(int(row["tg_chat_id"])), "archived", False))
        if CFG.mirror_dialog_state:
            entity = await self._entity(chat_id)
            await self._call(
                lambda: self.client.edit_folder(entity, folder=0 if archived else 1),
                peer=chat_id, label="archive",
            )
        await self._dialogs(force=True)
        return await self.get_chat(chat_id)

    async def clear_history(self, chat_id: str) -> dict:
        await self._enabled_row(chat_id)
        if CFG.destructive_clear_history:
            entity = await self._entity(chat_id)
            await self._call(
                lambda: self.client.delete_dialog(entity, revoke=False), peer=chat_id, label="clear"
            )
        else:
            # Non-destructive default: hide everything up to now in this client
            # only. Nothing is removed from Telegram.
            await db.execute(
                "UPDATE enabled_chats SET hide_before=? WHERE client_id=?", (db.now_ms(), chat_id)
            )
        await db.audit("clear_history", f"chat={chat_id} destructive={CFG.destructive_clear_history}")
        return {"chatId": chat_id}

    async def disable_chat(self, chat_id: str) -> dict:
        """deleteChat(): drops the chat out of the web client only.

        There is deliberately no call to LeaveChannel / DeleteChat / DeleteHistory
        anywhere in this method.
        """
        await db.execute("UPDATE enabled_chats SET enabled=0 WHERE client_id=?", (chat_id,))
        await db.audit("chat_disabled", f"chat={chat_id} via=web")
        HUB.emit("onChatRemoved", {"chatId": chat_id})
        return {"chatId": chat_id}

    async def create_chat(self, kind: str, title: str, member_ids: list[str]) -> dict:
        users = [await self._entity(m) for m in (member_ids or [])]
        if kind == "private":
            if not users:
                raise UserFacing("Pick someone to message first.")
            tg = await self._tg_id(member_ids[0])
            cid = await self._ensure_enabled(tg, "private", via="created")
            return await self.get_chat(cid)
        if kind == "channel":
            res = await self._call(
                lambda: self.client(functions.channels.CreateChannelRequest(
                    title=title or "Untitled", about="", megagroup=False)),
                label="create_channel",
            )
        else:
            if not users:
                raise UserFacing("A group needs at least one other member.")
            res = await self._call(
                lambda: self.client(functions.messages.CreateChatRequest(
                    users=users, title=title or "Untitled")),
                label="create_chat",
            )
        entity = next((c for c in getattr(res, "chats", []) or []), None)
        if entity is None:
            raise UserFacing("Telegram did not return the new chat.")
        await self._remember_entity(entity)
        tg = int(utils.get_peer_id(entity))
        cid = await self._ensure_enabled(tg, self._kind_of(entity), via="created")
        await self._dialogs(force=True)
        chat = await self.get_chat(cid)
        HUB.emit("onChatAdded", {"chat": chat})
        return chat

    async def add_members(self, chat_id: str, member_ids: list[str]) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        users = [await self._entity(m) for m in (member_ids or [])]
        if not users:
            raise UserFacing("Nobody was selected.")
        await self._call(
            lambda: self.client.add_chat_users(entity, users), peer=chat_id, label="add_members"
        )
        return await self.get_chat(chat_id)

    async def set_permission(self, chat_id: str, key: str, value: bool) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mapping = {"send": "send_messages", "media": "send_media",
                   "invite": "invite_users", "pin": "pin_messages"}
        if key not in mapping:
            raise UserFacing("That permission is not one Telegram understands.")
        try:
            await self._call(
                lambda: self.client.edit_permissions(entity, **{mapping[key]: bool(value)}),
                peer=chat_id, label="permissions",
            )
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("You do not have the rights to change that in this chat.") from None
        return await self.get_chat(chat_id)

    async def set_sign_messages(self, chat_id: str, value: bool) -> dict:
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        req = functions.channels.ToggleSignaturesRequest
        try:
            try:
                call = req(channel=entity, signatures_enabled=bool(value))
            except TypeError:
                call = req(channel=entity, enabled=bool(value))
            await self._call(lambda: self.client(call), peer=chat_id, label="signatures")
        except UserFacing:
            raise
        except Exception:
            raise UserFacing("Telegram would not change the signature setting on that channel.") from None
        return await self.get_chat(chat_id)

    async def block_user(self, user_id: str) -> dict:
        entity = await self._entity(user_id)
        await self._call(
            lambda: self.client(functions.contacts.BlockRequest(id=entity)), label="block"
        )
        return {"userId": user_id, "blocked": True}

    async def unblock_user(self, user_id: str) -> dict:
        entity = await self._entity(user_id)
        await self._call(
            lambda: self.client(functions.contacts.UnblockRequest(id=entity)), label="unblock"
        )
        return {"userId": user_id, "blocked": False}

    async def get_settings(self) -> dict:
        local = await settings_store.load()
        try:
            pw = await self._call(
                lambda: self.client(functions.account.GetPasswordRequest()),
                lane="low", label="GetPassword",
            )
            local["twoStep"] = bool(getattr(pw, "has_password", False))
        except Exception:
            pass
        key_map = {
            "lastSeen": types.InputPrivacyKeyStatusTimestamp,
            "profilePhoto": types.InputPrivacyKeyProfilePhoto,
            "calls": types.InputPrivacyKeyPhoneCall,
        }
        for name, key in key_map.items():
            try:
                res = await self._call(
                    lambda key=key: self.client(functions.account.GetPrivacyRequest(key=key())),
                    lane="low", label=f"privacy:{name}",
                )
                local[name] = self._privacy_to_setting(res.rules)
            except Exception:
                continue
        return local

    @staticmethod
    def _privacy_to_setting(rules: list) -> str:
        names = {type(r).__name__ for r in rules}
        if "PrivacyValueAllowAll" in names:
            return "everybody"
        if "PrivacyValueAllowContacts" in names:
            return "contacts"
        return "nobody"

    @staticmethod
    def _setting_to_privacy(value: str) -> list:
        if value == "everybody":
            return [types.InputPrivacyValueAllowAll()]
        if value == "contacts":
            return [types.InputPrivacyValueAllowContacts()]
        return [types.InputPrivacyValueDisallowAll()]

    async def update_settings(self, payload: dict) -> dict:
        patch = settings_store.sanitize(payload)
        local = {k: v for k, v in patch.items() if k in settings_store.UI_KEYS}
        if local:
            await settings_store.save(local)
        key_map = {
            "lastSeen": types.InputPrivacyKeyStatusTimestamp,
            "profilePhoto": types.InputPrivacyKeyProfilePhoto,
            "calls": types.InputPrivacyKeyPhoneCall,
        }
        for name, key in key_map.items():
            if name in patch:
                try:
                    await self._call(
                        lambda name=name, key=key: self.client(functions.account.SetPrivacyRequest(
                            key=key(), rules=self._setting_to_privacy(patch[name]))),
                        label=f"set_privacy:{name}",
                    )
                except Exception:
                    log.warning("could not apply the %s privacy rule", name)
        merged = await self.get_settings()
        HUB.emit("settings", merged)
        return merged

    async def update_profile(self, payload: dict) -> dict:
        payload = payload or {}
        if "name" in payload:
            name = (payload.get("name") or "").strip()
            first, _, last = name.partition(" ")
            await self._call(
                lambda: self.client(functions.account.UpdateProfileRequest(
                    first_name=first[:64], last_name=last[:64])),
                label="update_profile",
            )
        if "bio" in payload:
            await self._call(
                lambda: self.client(functions.account.UpdateProfileRequest(
                    about=(payload.get("bio") or "")[:70])),
                label="update_bio",
            )
        if "username" in payload:
            uname = (payload.get("username") or "").lstrip("@")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", uname or ""):
                try:
                    await self._call(
                        lambda: self.client(functions.account.UpdateUsernameRequest(username=uname)),
                        label="update_username",
                    )
                except UserFacing:
                    raise
                except Exception:
                    raise UserFacing("That username is not available.") from None
        self.me = await self.client.get_me()
        return await self._user_shape(self.me)

    # ----------------------------------------------------------------- media
    async def fetch_media(self, chat_id: str, message_id: str, variant: str):
        await self._enabled_row(chat_id)
        entity = await self._entity(chat_id)
        mid = await self._resolve_msg_id(chat_id, message_id)
        msg = await self._call(
            lambda: self.client.get_messages(entity, ids=mid), lane="low", label="media_msg"
        )
        if msg is None or msg.media is None:
            raise UserFacing("That attachment is no longer available.")
        size = getattr(getattr(msg, "document", None), "size", 0) or 0
        if size and size > CFG.media_max_download_bytes:
            raise UserFacing("That file is larger than this bridge is configured to fetch.")
        tmp = CFG.media_dir / f".dl-{ids.msg_cid(mid)}-{variant}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        thumb = 0 if variant == "thumb" else None
        path = await self._call(
            lambda: self.client.download_media(msg, file=str(tmp), thumb=thumb),
            lane="low", label="download",
        )
        if not path:
            raise UserFacing("Telegram did not return that file.")
        mime = getattr(getattr(msg, "document", None), "mime_type", None) or "image/jpeg"
        return path, mime

    async def fetch_avatar(self, entity_id: str, variant: str):
        entity = await self._entity(entity_id)
        tmp = CFG.media_dir / f".av-{entity_id}-{variant}"
        path = await self._call(
            lambda: self.client.download_profile_photo(
                entity, file=str(tmp), download_big=(variant == "full")),
            lane="low", label="avatar",
        )
        if not path:
            raise UserFacing("No profile photo.")
        return path, "image/jpeg"

    # -------------------------------------------------------------- commands
    async def _ensure_enabled(self, tg_chat_id: int, kind: str, via: str = "addweb") -> str:
        cid = ids.chat_cid(tg_chat_id, kind, self.me_id)
        row = await db.fetchone("SELECT * FROM enabled_chats WHERE tg_chat_id=?", (tg_chat_id,))
        if row and row["enabled"]:
            return row["client_id"]
        if row:
            await db.execute(
                "UPDATE enabled_chats SET enabled=1, added_at=?, added_via=? WHERE tg_chat_id=?",
                (db.now_ms(), via, tg_chat_id),
            )
        else:
            await db.execute(
                "INSERT INTO enabled_chats(client_id, tg_chat_id, kind, enabled, added_at, added_via)"
                " VALUES(?,?,?,1,?,?)",
                (cid, tg_chat_id, kind, db.now_ms(), via),
            )
        await db.audit("chat_enabled", f"chat={cid} via={via}")
        return cid

    def _register_handlers(self) -> None:
        client = self.client

        @client.on(events.NewMessage(outgoing=True))
        async def _on_outgoing_command(event):
            try:
                await self._maybe_command(event)
            except Exception:
                log.exception("command handling failed")

        @client.on(events.NewMessage)
        async def _on_new(event):
            try:
                await self._on_new_message(event)
            except Exception:
                log.exception("new-message fan-out failed")

        @client.on(events.MessageEdited)
        async def _on_edit(event):
            cid = await self._enabled_cid(event)
            if not cid or await self._is_filtered(int(utils.get_peer_id(event.message.peer_id)), int(event.message.id)):
                return
            HUB.emit("onMessageEdited", {
                "chatId": cid, "message": await self._message_shape(event.message, cid)
            })

        @client.on(events.MessageDeleted)
        async def _on_delete(event):
            cid = await self._enabled_cid(event)
            if not cid:
                return
            for mid in event.deleted_ids:
                HUB.emit("onMessageDeleted", {
                    "chatId": cid, "messageId": ids.msg_cid(int(mid)), "forEveryone": True
                })

        @client.on(events.MessageRead(inbox=False))
        async def _on_read(event):
            cid = await self._enabled_cid(event)
            if not cid:
                return
            HUB.emit("onReadReceipt", {
                "chatId": cid, "messageId": ids.msg_cid(int(event.max_id)), "status": "read"
            })

        @client.on(events.UserUpdate)
        async def _on_user(event):
            uid = getattr(event, "user_id", None)
            if not uid:
                return
            cid = ids.user_cid(int(uid), self.me_id)
            if getattr(event, "typing", False) or getattr(event, "uploading", False):
                await self._emit_typing(event, cid)
            elif getattr(event, "online", None) is not None or getattr(event, "last_seen", None):
                HUB.emit("onPresence", {
                    "userId": cid,
                    "online": bool(getattr(event, "online", False)),
                    "lastSeen": int(event.last_seen.timestamp() * 1000) if getattr(event, "last_seen", None) else None,
                })

        @client.on(events.Raw(types.UpdateMessageReactions))
        async def _on_reactions(update):
            try:
                tg_chat = int(utils.get_peer_id(update.peer))
                row = await db.fetchone(
                    "SELECT client_id FROM enabled_chats WHERE tg_chat_id=? AND enabled=1", (tg_chat,)
                )
                if not row:
                    return
                entity = await self.client.get_input_entity(tg_chat)
                msg = await self.client.get_messages(entity, ids=int(update.msg_id))
                if msg is None:
                    return
                HUB.emit("onReaction", {
                    "chatId": row["client_id"],
                    "messageId": ids.msg_cid(int(update.msg_id)),
                    "reactions": await self._reactions_shape(msg),
                })
            except Exception:
                log.debug("reaction update skipped", exc_info=True)

    async def _emit_typing(self, event, user_cid: str) -> None:
        cid = await self._enabled_cid(event)
        if not cid:
            return
        try:
            sender = await event.get_user()
            name = utils.get_display_name(sender) or "Someone"
        except Exception:
            name = "Someone"
        HUB.emit("onTyping", {"chatId": cid, "userId": user_cid, "userName": name, "typing": True})
        key = (cid, user_cid)
        old = self._typing_timers.pop(key, None)
        if old:
            old.cancel()

        async def expire():
            await asyncio.sleep(TYPING_TTL)
            HUB.emit("onTyping", {"chatId": cid, "userId": user_cid, "userName": name, "typing": False})
            self._typing_timers.pop(key, None)

        self._typing_timers[key] = asyncio.create_task(expire())

    async def _enabled_cid(self, event) -> Optional[str]:
        try:
            peer = getattr(event, "peer_id", None) or getattr(getattr(event, "message", None), "peer_id", None)
            if peer is None and getattr(event, "chat_id", None):
                tg = int(event.chat_id)
            else:
                tg = int(utils.get_peer_id(peer))
        except Exception:
            return None
        row = await db.fetchone(
            "SELECT client_id FROM enabled_chats WHERE tg_chat_id=? AND enabled=1", (tg,)
        )
        return row["client_id"] if row else None

    async def _maybe_command(self, event) -> None:
        text = (event.message.message or "").strip()
        m = self._cmd.match(text)
        if not m:
            return
        cmd = m.group(1).lower()
        entity = await event.get_chat()
        tg = int(utils.get_peer_id(event.message.peer_id))
        kind = "saved" if tg == self.me_id else self._kind_of(entity)
        await self._remember_entity(entity)

        # The command message itself never reaches the client, in history or live.
        await db.execute(
            "INSERT OR IGNORE INTO filtered_msgs(tg_chat_id, tg_msg_id, reason, at) VALUES(?,?,?,?)",
            (tg, int(event.message.id), cmd, db.now_ms()),
        )
        if CFG.delete_command_messages:
            try:
                await event.message.delete()
            except Exception:
                log.info("could not delete the command message")

        if cmd == "addweb":
            existing = await db.fetchone(
                "SELECT enabled FROM enabled_chats WHERE tg_chat_id=?", (tg,)
            )
            if existing and existing["enabled"]:
                return  # idempotent: no duplicate row in the client
            cid = await self._ensure_enabled(tg, kind, via="addweb")
            await self._dialogs(force=True)
            HUB.emit("onChatAdded", {"chat": await self.get_chat(cid)})
        else:
            row = await db.fetchone("SELECT client_id FROM enabled_chats WHERE tg_chat_id=?", (tg,))
            if not row:
                return
            await db.execute("UPDATE enabled_chats SET enabled=0 WHERE tg_chat_id=?", (tg,))
            await db.audit("chat_disabled", f"chat={row['client_id']} via=delweb")
            HUB.emit("onChatRemoved", {"chatId": row["client_id"]})

    async def _on_new_message(self, event) -> None:
        msg = event.message
        tg = int(utils.get_peer_id(msg.peer_id))
        if self._cmd.match((msg.message or "").strip()):
            return  # command text never becomes a bubble, even from another device
        if await self._is_filtered(tg, int(msg.id)):
            return
        row = await db.fetchone(
            "SELECT client_id, hide_before FROM enabled_chats WHERE tg_chat_id=? AND enabled=1", (tg,)
        )
        if not row:
            return
        if int(msg.date.timestamp() * 1000) < int(row["hide_before"]):
            return
        self._dialog_cache_at = 0.0
        shape = await self._message_shape(msg, row["client_id"])
        HUB.emit("onNewMessage", {"chatId": row["client_id"], "message": shape})

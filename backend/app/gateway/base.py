"""The gateway seam.

Everything the HTTP tier needs from Telegram is behind this interface, which is
what lets the MTProto client run in a different process (or be swapped for the
Fake gateway in tests) without the routes knowing.
"""
from __future__ import annotations

from typing import Any


class UserFacing(Exception):
    """An error whose message is safe to render verbatim to a person.

    BACKEND.md: errors are values, not throws, and the string is shown to the
    user. Anything not wrapped in this becomes a generic message instead.
    """


class Gateway:
    name = "base"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # --- identity ----------------------------------------------------------
    async def bootstrap(self) -> dict[str, Any]: raise NotImplementedError

    # --- reads -------------------------------------------------------------
    async def get_chats(self) -> list[dict]: raise NotImplementedError
    async def get_chat(self, chat_id: str) -> dict: raise NotImplementedError
    async def get_messages(self, chat_id: str, before: int | None, limit: int) -> list[dict]: raise NotImplementedError
    async def get_profile(self, user_id: str) -> dict: raise NotImplementedError
    async def get_shared_media(self, chat_id: str, kind: str | None) -> Any: raise NotImplementedError
    async def search_all(self, query: str) -> dict: raise NotImplementedError
    async def search_in_chat(self, chat_id: str, query: str) -> list[dict]: raise NotImplementedError
    async def get_contacts(self) -> list[dict]: raise NotImplementedError
    async def get_settings(self) -> dict: raise NotImplementedError
    async def get_blocked(self) -> list[dict]: raise NotImplementedError
    async def get_sessions(self) -> list[dict]: raise NotImplementedError

    # --- writes ------------------------------------------------------------
    async def send_message(self, chat_id: str, payload: dict) -> dict: raise NotImplementedError
    async def edit_message(self, chat_id: str, message_id: str, text: str) -> dict: raise NotImplementedError
    async def delete_message(self, chat_id: str, message_id: str, for_everyone: bool) -> dict: raise NotImplementedError
    async def react(self, chat_id: str, message_id: str, emoji: str) -> dict: raise NotImplementedError
    async def forward(self, from_chat: str, message_ids: list[str], to_chat: str) -> dict: raise NotImplementedError
    async def vote_poll(self, chat_id: str, message_id: str, index: int) -> dict: raise NotImplementedError
    async def pin_message(self, chat_id: str, message_id: str) -> dict: raise NotImplementedError
    async def mark_read(self, chat_id: str) -> dict: raise NotImplementedError
    async def set_typing(self, chat_id: str) -> dict: raise NotImplementedError
    async def save_draft(self, chat_id: str, text: str) -> dict: raise NotImplementedError
    async def pin_chat(self, chat_id: str) -> dict: raise NotImplementedError
    async def mute_chat(self, chat_id: str) -> dict: raise NotImplementedError
    async def archive_chat(self, chat_id: str) -> dict: raise NotImplementedError
    async def clear_history(self, chat_id: str) -> dict: raise NotImplementedError
    async def disable_chat(self, chat_id: str) -> dict: raise NotImplementedError
    async def create_chat(self, kind: str, title: str, member_ids: list[str]) -> dict: raise NotImplementedError
    async def add_members(self, chat_id: str, ids: list[str]) -> dict: raise NotImplementedError
    async def set_permission(self, chat_id: str, key: str, value: bool) -> dict: raise NotImplementedError
    async def set_sign_messages(self, chat_id: str, value: bool) -> dict: raise NotImplementedError
    async def block_user(self, user_id: str) -> dict: raise NotImplementedError
    async def unblock_user(self, user_id: str) -> dict: raise NotImplementedError
    async def update_settings(self, payload: dict) -> dict: raise NotImplementedError
    async def update_profile(self, payload: dict) -> dict: raise NotImplementedError
    async def terminate_session(self, session_id: str) -> dict: raise NotImplementedError

    # --- media -------------------------------------------------------------
    async def fetch_media(self, chat_id: str, message_id: str, variant: str) -> tuple[bytes | str, str]:
        """Returns (path-or-bytes, mime)."""
        raise NotImplementedError

    async def fetch_avatar(self, entity_id: str, variant: str) -> tuple[bytes | str, str]:
        raise NotImplementedError

"""WebSocket hub: one fan-out point for every update handler in BACKEND.md."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("hub")

HANDLER_NAMES = (
    "onNewMessage",
    "onMessageEdited",
    "onMessageDeleted",
    "onReaction",
    "onTyping",
    "onPresence",
    "onReadReceipt",
    "onChatAdded",
    "onChatRemoved",
    "onConnectionChange",
)


class Hub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self.status = "connecting"

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        if event not in HANDLER_NAMES and event != "settings":
            log.debug("dropping unknown event %s", event)
            return
        frame = {"event": event, "payload": payload or {}}
        for q in list(self._clients):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # A stalled tab must not back-pressure Telegram updates.
                self._clients.discard(q)
                log.warning("dropped a websocket client that stopped draining")

    def set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        log.info("connection status -> %s", status)
        self.emit("onConnectionChange", {"status": status})


HUB = Hub()

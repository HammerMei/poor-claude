"""Per-session MCP Channels routing primitives.

This module intentionally models routing without depending on Claude Code being
present. The real MCP transport can later adapt `ChannelNotification` objects to
JSON-RPC writes on each session's MCP connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


CHANNEL_NAME = "poor-claude"


@dataclass(frozen=True)
class ChannelNotification:
    route_key: str
    session_id: str
    request_id: str
    prompt: str
    channel: str = CHANNEL_NAME

    def json_rpc(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "content": self.prompt,
                "meta": {"request_id": self.request_id},
            },
        }


def wrap_prompt(*, request_id: str, prompt: str) -> str:
    return f'<poor-claude-request id="{request_id}">\n{prompt}\n</poor-claude-request>'


class SessionChannelQueue:
    def __init__(self, route_key: str) -> None:
        self.route_key = route_key
        self._queue: asyncio.Queue[ChannelNotification] = asyncio.Queue()

    async def put(self, notification: ChannelNotification) -> None:
        if notification.route_key != self.route_key:
            raise ValueError("notification route_key does not match queue")
        await self._queue.put(notification)

    async def get(self) -> ChannelNotification:
        return await self._queue.get()

    def get_nowait(self) -> ChannelNotification:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()


class McpRouter:
    def __init__(self) -> None:
        self._queues: dict[str, SessionChannelQueue] = {}

    def ensure_route(self, route_key: str) -> SessionChannelQueue:
        queue = self._queues.get(route_key)
        if queue is None:
            queue = SessionChannelQueue(route_key)
            self._queues[route_key] = queue
        return queue

    def ensure_session(self, session_id: str) -> SessionChannelQueue:
        return self.ensure_route(session_id)

    def remove_route(self, route_key: str) -> None:
        self._queues.pop(route_key, None)

    def remove_session(self, session_id: str) -> None:
        self.remove_route(session_id)

    async def route_prompt(
        self,
        *,
        route_key: str,
        session_id: str,
        request_id: str,
        prompt: str,
    ) -> ChannelNotification:
        notification = ChannelNotification(
            route_key=route_key,
            session_id=session_id,
            request_id=request_id,
            prompt=wrap_prompt(request_id=request_id, prompt=prompt),
        )
        await self.ensure_route(route_key).put(notification)
        return notification

    def queue_for(self, route_key: str) -> SessionChannelQueue | None:
        return self._queues.get(route_key)

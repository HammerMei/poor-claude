"""Minimal MCP Channels server helpers.

The full Claude Code stdio lifecycle will be wired in a later step. For now this
module provides the capability declaration and JSON-RPC notification formatting
that the transport layer will use.
"""

from __future__ import annotations

from typing import Any

from poor_claude.mcp_router import ChannelNotification


def capabilities() -> dict[str, Any]:
    return {"experimental": {"claude/channel": {}}}


def notification_to_json_rpc(notification: ChannelNotification) -> dict[str, Any]:
    return notification.json_rpc()

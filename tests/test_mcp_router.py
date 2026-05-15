import asyncio

import pytest

from poor_claude.mcp_router import McpRouter, wrap_prompt
from poor_claude.mcp_server import capabilities, notification_to_json_rpc


def test_capabilities_declares_claude_channel() -> None:
    assert capabilities() == {"experimental": {"claude/channel": {}}}


def test_wrap_prompt_includes_request_id() -> None:
    wrapped = wrap_prompt(request_id="req1", prompt="hello")
    assert '<poor-claude-request id="req1">' in wrapped
    assert "hello" in wrapped


def test_router_keeps_session_queues_isolated() -> None:
    async def run() -> None:
        router = McpRouter()
        notification_a = await router.route_prompt(
            route_key="/tmp/a::same-session",
            session_id="a",
            request_id="req-a",
            prompt="say A",
        )
        notification_b = await router.route_prompt(
            route_key="/tmp/b::same-session",
            session_id="b",
            request_id="req-b",
            prompt="say B",
        )

        queue_a = router.queue_for("/tmp/a::same-session")
        queue_b = router.queue_for("/tmp/b::same-session")
        assert queue_a is not None
        assert queue_b is not None
        assert queue_a.get_nowait() == notification_a
        assert queue_b.get_nowait() == notification_b
        assert queue_a.empty()
        assert queue_b.empty()

    asyncio.run(run())


def test_notification_json_rpc_shape() -> None:
    async def run() -> None:
        router = McpRouter()
        notification = await router.route_prompt(
            route_key="/tmp/demo::demo",
            session_id="demo",
            request_id="req1",
            prompt="hello",
        )
        json_rpc = notification_to_json_rpc(notification)
        assert json_rpc["method"] == "notifications/claude/channel"
        assert json_rpc["params"]["meta"] == {"request_id": "req1"}
        assert "req1" in json_rpc["params"]["content"]

    asyncio.run(run())


def test_queue_rejects_wrong_session() -> None:
    async def run() -> None:
        router = McpRouter()
        notification = await router.route_prompt(
            route_key="/tmp/a::a",
            session_id="a",
            request_id="req-a",
            prompt="say A",
        )
        queue_b = router.ensure_route("/tmp/b::b")
        with pytest.raises(ValueError, match="does not match"):
            await queue_b.put(notification)

    asyncio.run(run())

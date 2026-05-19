import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

from poor_claude.hooks import posttool_hook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_capture_server():
    """Start a minimal HTTP server that records POSTs and returns 200."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):  # noqa: ANN002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


# ---------------------------------------------------------------------------
# Background agent launch → POSTs to callback
# ---------------------------------------------------------------------------


def test_posttool_hook_posts_when_bg_agent_launched(monkeypatch) -> None:
    """Background Agent PostToolUse (isAsync=true, agentId present) posts to callback URL."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"isAsync": True, "agentId": "agent-abc"},
            "session_id": "sess-001",
            "cwd": "/tmp/proj",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert len(received) == 1
    assert received[0]["session_id"] == "sess-001"
    assert received[0]["agent_id"] == "agent-abc"
    assert received[0]["cwd"] == "/tmp/proj"
    server.shutdown()


# ---------------------------------------------------------------------------
# Sync agent (no isAsync) → does NOT post
# ---------------------------------------------------------------------------


def test_posttool_hook_does_not_post_for_sync_agent(monkeypatch) -> None:
    """Sync agent PostToolUse (no isAsync field) must NOT post to callback."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"status": "completed", "agentId": "agent-sync"},
            "session_id": "sess-001",
            "cwd": "/tmp/proj",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


def test_posttool_hook_does_not_post_for_is_async_false(monkeypatch) -> None:
    """isAsync=false must NOT post."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"isAsync": False, "agentId": "agent-sync"},
            "session_id": "sess-001",
            "cwd": "/tmp/proj",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


# ---------------------------------------------------------------------------
# Non-Agent tool → early exit, no POST
# ---------------------------------------------------------------------------


def test_posttool_hook_ignores_non_agent_tools(monkeypatch) -> None:
    """A PostToolUse event for a non-Agent tool is ignored."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Bash",
            "tool_response": {"isAsync": True, "agentId": "irrelevant"},
            "session_id": "sess-001",
            "cwd": "/tmp/proj",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


# ---------------------------------------------------------------------------
# Edge cases: empty / bad stdin
# ---------------------------------------------------------------------------


def test_posttool_hook_handles_empty_stdin(monkeypatch) -> None:
    """Empty stdin returns 0 (best-effort, never blocks)."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr("sys.stdin", StringIO(""))
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


def test_posttool_hook_handles_json_decode_error(monkeypatch, capsys) -> None:
    """Invalid JSON returns 0 and logs to stderr."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr("sys.stdin", StringIO("not-json{{{"))
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    captured = capsys.readouterr()
    assert "posttool hook" in captured.err
    server.shutdown()


# ---------------------------------------------------------------------------
# Missing agentId → logs warning, returns 0
# ---------------------------------------------------------------------------


def test_posttool_hook_missing_agent_id_logs_and_returns_0(monkeypatch, capsys) -> None:
    """Background Agent without agentId logs to stderr and returns 0."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"isAsync": True},  # no agentId
            "session_id": "sess-001",
            "cwd": "/tmp",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    captured = capsys.readouterr()
    assert "agentId" in captured.err or "agent" in captured.err.lower()
    server.shutdown()


# ---------------------------------------------------------------------------
# Missing session_id → returns 0 silently
# ---------------------------------------------------------------------------


def test_posttool_hook_missing_session_id_returns_0(monkeypatch) -> None:
    """Background Agent without session_id returns 0 without posting."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/agent-launched"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"isAsync": True, "agentId": "agent-xyz"},
            # no session_id
            "cwd": "/tmp",
        })),
    )
    result = posttool_hook.main(["--poor-claude-managed", "--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


# ---------------------------------------------------------------------------
# Server unreachable → best-effort, returns 0
# ---------------------------------------------------------------------------


def test_posttool_hook_swallows_connection_error(monkeypatch, capsys) -> None:
    """Connection failure is swallowed; hook still returns 0 and logs to stderr."""
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(json.dumps({
            "tool_name": "Agent",
            "tool_response": {"isAsync": True, "agentId": "agent-abc"},
            "session_id": "sess-001",
            "cwd": "/tmp",
        })),
    )
    result = posttool_hook.main([
        "--poor-claude-managed",
        "--callback-url",
        "http://127.0.0.1:1/hook/agent-launched",
    ])
    assert result == 0
    captured = capsys.readouterr()
    assert "agent-launched notification failed" in captured.err

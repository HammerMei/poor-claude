"""Tests for poor_claude.hooks.subagent_stop_hook."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

import pytest

from poor_claude.hooks import subagent_stop_hook
from poor_claude.hooks.subagent_stop_hook import main


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


def test_subagent_stop_hook_posts_session_id_and_cwd(monkeypatch) -> None:
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/subagent-stop"
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            '{"session_id":"test-parent-session","cwd":"/workspace/project",'
            '"agent_id":"sub-agent-id","agent_type":"general-purpose"}'
        ),
    )
    result = main(["--callback-url", url])
    assert result == 0
    assert len(received) == 1
    assert received[0]["session_id"] == "test-parent-session"
    assert received[0]["cwd"] == "/workspace/project"
    server.shutdown()


def test_subagent_stop_hook_returns_0_on_missing_session_id(monkeypatch) -> None:
    """If session_id is absent, hook exits 0 silently (nothing to notify)."""
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/subagent-stop"
    monkeypatch.setattr("sys.stdin", StringIO('{"agent_id":"sub-agent-id"}'))
    result = main(["--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


def test_subagent_stop_hook_handles_empty_stdin(monkeypatch) -> None:
    server, received = _start_capture_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook/subagent-stop"
    monkeypatch.setattr("sys.stdin", StringIO(""))
    result = main(["--callback-url", url])
    assert result == 0
    assert received == []
    server.shutdown()


def test_subagent_stop_hook_requires_callback_url() -> None:
    with pytest.raises(SystemExit):
        main([])

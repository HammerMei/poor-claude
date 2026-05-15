"""Minimal stdio MCP server for poor-claude Channels validation.

This intentionally avoids external MCP SDK dependencies for the POC. It speaks
line-delimited JSON-RPC over stdio, which is enough to validate whether Claude
Code starts the server and performs the MCP initialize handshake.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

from poor_claude.mcp_server import capabilities


SERVER_NAME = "poor-claude"
SERVER_VERSION = "0.1.0"


def log_event(event: dict[str, Any]) -> None:
    log_path = os.environ.get("POOR_CLAUDE_MCP_LOG")
    if not log_path:
        return
    payload = {"ts": time.time(), **event}
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def make_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def write_message(message: dict[str, Any], *, lock: threading.Lock) -> None:
    with lock:
        print(json.dumps(message), flush=True)


def maybe_start_channel_poller(*, lock: threading.Lock, started: threading.Event) -> None:
    if started.is_set():
        return
    control_url = os.environ.get("POOR_CLAUDE_CONTROL_URL")
    route_key = os.environ.get("POOR_CLAUDE_ROUTE_KEY")
    if not control_url or not route_key:
        return
    started.set()
    thread = threading.Thread(
        target=_poll_channel_notifications,
        args=(control_url.rstrip("/"), route_key, lock),
        daemon=True,
    )
    thread.start()


def _poll_channel_notifications(control_url: str, route_key: str, lock: threading.Lock) -> None:
    url = f"{control_url}/mcp/next?route_key={quote(route_key, safe='')}"
    while True:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - localhost POC daemon
                payload = json.loads(response.read().decode("utf-8"))
            notification = payload.get("notification")
            if notification is not None:
                log_event({"direction": "out", "message": notification})
                write_message(notification, lock=lock)
            else:
                time.sleep(0.2)
        except Exception as exc:
            log_event({"event": "poll_error", "error": str(exc)})
            time.sleep(1.0)


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    log_event({"direction": "in", "message": message})

    if method == "initialize":
        return make_result(
            request_id,
            {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": capabilities(),
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return make_result(request_id, {"tools": []})
    if method == "prompts/list":
        return make_result(request_id, {"prompts": []})
    if method == "resources/list":
        return make_result(request_id, {"resources": []})
    if method and request_id is None:
        # Notification; no response.
        return None
    return make_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    log_event({"event": "server_start", "pid": os.getpid()})
    write_lock = threading.Lock()
    poller_started = threading.Event()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message: dict[str, Any] | None = None
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("JSON-RPC message must be an object")
            message = parsed
            response = handle_request(message)
        except Exception as exc:
            response = make_error(None, -32700, str(exc))
        if response is not None:
            log_event({"direction": "out", "message": response})
            write_message(response, lock=write_lock)
        if message is not None and message.get("method") == "notifications/initialized":
            maybe_start_channel_poller(lock=write_lock, started=poller_started)
    log_event({"event": "server_stop"})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

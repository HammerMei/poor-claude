"""Local control daemon for poor-claude."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from poor_claude.daemon import DaemonState, write_state
from poor_claude.launcher import build_claude_command, prepare_launch_spec
from poor_claude.mcp_router import McpRouter
from poor_claude.process_manager import ProcessManager
from poor_claude.session import SessionRegistry


class ControlState:
    def __init__(self, *, state_dir: Path | None = None, callback_base_url: str = "http://127.0.0.1") -> None:
        self.registry = SessionRegistry()
        self.mcp_router = McpRouter()
        self.process_manager = ProcessManager()
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.shutdown_requested = False
        self.state_dir = state_dir or Path.home() / ".poor-claude"
        self.callback_base_url = callback_base_url


def _bool_metadata(value: str | None) -> bool:
    return value == "True"


def _apply_launch_metadata(session, payload: dict[str, Any]) -> None:
    """Record immutable launch-affecting metadata for a session route."""
    incoming_settings = payload.get("settings_path")
    incoming_settings = incoming_settings if isinstance(incoming_settings, str) else ""
    incoming_skip = bool(payload.get("dangerously_skip_permissions", False))
    incoming_dev_channels = bool(payload.get("dangerously_load_development_channels", True))
    incoming_auto_trust = bool(payload.get("auto_accept_workspace_trust", False))
    if not incoming_dev_channels:
        raise RuntimeError("poor-claude requires development channels for interactive routing")

    existing_settings = session.metadata.get("settings_path", "")
    existing_skip = _bool_metadata(session.metadata.get("dangerously_skip_permissions"))
    existing_dev_channels = _bool_metadata(
        session.metadata.get("dangerously_load_development_channels", "True")
    )
    existing_auto_trust = _bool_metadata(session.metadata.get("auto_accept_workspace_trust"))

    if "launch_config_frozen" in session.metadata:
        mismatches = []
        if existing_settings != incoming_settings:
            mismatches.append(f"settings_path existing={existing_settings!r} incoming={incoming_settings!r}")
        if existing_skip != incoming_skip:
            mismatches.append(
                f"dangerously_skip_permissions existing={existing_skip!r} incoming={incoming_skip!r}"
            )
        if existing_dev_channels != incoming_dev_channels:
            mismatches.append(
                "dangerously_load_development_channels "
                f"existing={existing_dev_channels!r} incoming={incoming_dev_channels!r}"
            )
        if existing_auto_trust != incoming_auto_trust:
            mismatches.append(
                f"auto_accept_workspace_trust existing={existing_auto_trust!r} incoming={incoming_auto_trust!r}"
            )
        if mismatches:
            raise RuntimeError(
                "existing session launch config differs from request; stop/recreate session or use matching flags: "
                + "; ".join(mismatches)
            )
        return

    session.metadata["settings_path"] = incoming_settings
    session.metadata["dangerously_skip_permissions"] = str(incoming_skip)
    session.metadata["dangerously_load_development_channels"] = str(incoming_dev_channels)
    session.metadata["auto_accept_workspace_trust"] = str(incoming_auto_trust)
    session.metadata["launch_config_frozen"] = "True"


def _prepare_launch_metadata(state: ControlState, session) -> None:
    if "merged_settings_path" in session.metadata and "launch_command" in session.metadata:
        return
    spec = prepare_launch_spec(
        session=session,
        state_dir=state.state_dir,
        callback_base_url=state.callback_base_url,
    )
    session.metadata["launch_command"] = json.dumps(build_claude_command(spec))


def _ensure_process_metadata(state: ControlState, session) -> None:
    spec = prepare_launch_spec(
        session=session,
        state_dir=state.state_dir,
        callback_base_url=state.callback_base_url,
    )
    managed = state.process_manager.ensure_running(route_key=session.route_key, spec=spec)
    session.metadata["process_pid"] = str(managed.process.pid)
    session.metadata["process_alive"] = str(managed.is_alive())


def make_handler(state: ControlState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "poor-claude-control/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/mcp/next":
                route_key = parse_qs(parsed.query).get("route_key", [""])[0]
                with state.lock:
                    queue = state.mcp_router.queue_for(route_key)
                    notification = None if queue is None or queue.empty() else queue.get_nowait()
                self._send_json(
                    200,
                    {"notification": notification.json_rpc() if notification is not None else None},
                )
                return
            if self.path == "/healthz":
                self._send_json(200, {"ok": True})
                return
            if self.path == "/sessions":
                with state.lock:
                    for session in state.registry._sessions.values():
                        managed = state.process_manager.get(session.route_key)
                        if managed is not None:
                            session.metadata["process_pid"] = str(managed.process.pid)
                            session.metadata["process_alive"] = str(managed.is_alive())
                        elif "process_pid" in session.metadata:
                            session.metadata["process_alive"] = "False"
                    sessions = [
                        {
                            "session_id": session.session_id,
                            "route_key": session.route_key,
                            "auto_created": session.auto_created,
                            "ttl_seconds": session.ttl_seconds,
                            "keep_alive": session.keep_alive,
                            "workdir": session.workdir,
                            "active_request": session.active_request.request_id
                            if session.active_request
                            else None,
                            "metadata": session.metadata,
                        }
                        for session in state.registry._sessions.values()
                    ]
                self._send_json(200, {"sessions": sessions})
                return
            self._send_error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/requests":
                self._handle_request()
                return
            if self.path == "/sessions":
                self._handle_create_session()
                return
            if self.path == "/hook/stop":
                self._handle_stop_hook()
                return
            if self.path == "/shutdown":
                state.process_manager.stop_all()
                state.shutdown_requested = True
                self._send_json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._send_error(404, "not found")

        def _handle_create_session(self) -> None:
            try:
                payload = self._read_json()
                session_id = payload.get("session_id")
                ttl_seconds = payload.get("ttl_seconds")
                keep_alive = bool(payload.get("keep_alive", False))
                workdir = str(payload.get("workdir", os.getcwd()))
                with state.lock:
                    session = state.registry.create_or_get(
                        session_id=session_id if isinstance(session_id, str) else None,
                        ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                        keep_alive=keep_alive,
                        workdir=workdir,
                    )
                    _apply_launch_metadata(session, payload)
                    _prepare_launch_metadata(state, session)
                    if bool(payload.get("launch_process", False)):
                        _ensure_process_metadata(state, session)
                    state.mcp_router.ensure_route(session.route_key)
                self._send_json(
                    201,
                    {
                        "session_id": session.session_id,
                        "auto_created": session.auto_created,
                        "ttl_seconds": session.ttl_seconds,
                        "keep_alive": session.keep_alive,
                    },
                )
            except Exception as exc:
                self._send_error(400, str(exc))

        def do_DELETE(self) -> None:  # noqa: N802
            prefix = "/sessions/"
            if not self.path.startswith(prefix):
                self._send_error(404, "not found")
                return
            session_id = self.path[len(prefix) :]
            workdir = self.headers.get("X-Poor-Claude-Workdir", os.getcwd())
            with state.lock:
                existing = state.registry.get(session_id, workdir=workdir)
                removed = None
                if existing is not None:
                    removed = state.registry._sessions.pop(existing.route_key, None)
                    state.mcp_router.remove_route(existing.route_key)
                    state.process_manager.stop(existing.route_key)
            if removed is None:
                self._send_error(404, "session not found")
                return
            self._send_json(200, {"ok": True, "session_id": session_id})

        def _handle_request(self) -> None:
            try:
                payload = self._read_json()
                session_id = payload.get("session_id")
                prompt = payload["prompt"]
                timeout_seconds = int(payload.get("timeout_seconds", 300))
                wait_for_response = bool(payload.get("wait_for_response", False))
                ttl_seconds = payload.get("ttl_seconds")
                keep_alive = bool(payload.get("keep_alive", False))
                workdir = str(payload.get("workdir", os.getcwd()))
                with state.lock:
                    session = state.registry.create_or_get(
                        session_id=session_id if isinstance(session_id, str) else None,
                        ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                        keep_alive=keep_alive,
                        workdir=workdir,
                    )
                    _apply_launch_metadata(session, payload)
                    _prepare_launch_metadata(state, session)
                    if bool(payload.get("launch_process", False)):
                        _ensure_process_metadata(state, session)
                    request = state.registry.start_request_for_route(
                        route=session.route_key,
                        prompt=str(prompt),
                        timeout_seconds=timeout_seconds,
                    )
                    notification = asyncio.run(
                        state.mcp_router.route_prompt(
                            route_key=session.route_key,
                            session_id=session.session_id,
                            request_id=request.request_id,
                            prompt=str(prompt),
                        )
                    )
                    if wait_for_response:
                        deadline = time.time() + timeout_seconds
                        response = None
                        while True:
                            active = session.active_request
                            if active is None:
                                response = request.response
                                break
                            remaining = deadline - time.time()
                            if remaining <= 0:
                                state.registry.timeout_request_for_route(
                                    route=session.route_key,
                                    request_id=request.request_id,
                                )
                                state.condition.notify_all()
                                raise TimeoutError("timed out waiting for Claude response")
                            state.condition.wait(timeout=remaining)
                        self._send_json(
                            200,
                            {
                                "request_id": request.request_id,
                                "session_id": session.session_id,
                                "route_key": session.route_key,
                                "status": "completed",
                                "response": response or "",
                            },
                        )
                        return
                self._send_json(
                    202,
                    {
                        "request_id": request.request_id,
                        "session_id": session.session_id,
                        "route_key": session.route_key,
                        "status": "queued",
                        "channel_notification": notification.json_rpc(),
                    },
                )
            except Exception as exc:
                self._send_error(400, str(exc))

        def _handle_stop_hook(self) -> None:
            try:
                payload = self._read_json()
                session_id = str(payload["session_id"])
                request_id = payload.get("request_id")
                response = str(payload.get("response", ""))
                workdir = str(payload.get("cwd") or payload.get("workdir") or os.getcwd())
                with state.lock:
                    session = state.registry.get(session_id, workdir=workdir)
                    if session is None:
                        raise RuntimeError("session route not found for Stop hook")
                    if not isinstance(request_id, str):
                        raise RuntimeError("Stop hook payload missing request_id")
                    state.registry.finish_request_for_route(
                        route=session.route_key,
                        request_id=request_id,
                        response=response,
                    )
                    state.condition.notify_all()
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_error(400, str(exc))

    return Handler


def serve(*, state_file: Path, host: str = "127.0.0.1", port: int = 0) -> int:
    state = ControlState(state_dir=state_file.parent)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.timeout = 0.5
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    write_state(state_file, DaemonState(pid=os.getpid(), address=address))

    def remove_state(*_: object) -> None:
        state_file.unlink(missing_ok=True)

    def request_shutdown(*_: object) -> None:
        state.shutdown_requested = True

    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        while not state.shutdown_requested:
            server.handle_request()
    finally:
        state.process_manager.stop_all()
        remove_state()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    return serve(state_file=Path(args.state_file), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

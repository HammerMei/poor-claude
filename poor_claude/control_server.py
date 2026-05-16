"""Local control daemon for poor-claude."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from poor_claude.daemon import DaemonState, write_state
from poor_claude.launcher import build_claude_command, cleanup_project_mcp_config, prepare_launch_spec
from poor_claude.mcp_router import McpRouter
from poor_claude.process_manager import ProcessManager
from poor_claude.settings import cleanup_project_local_settings
from poor_claude.session import SessionRegistry
from poor_claude.transcript import read_response_record_after_request_from_file, transcript_candidates


class ControlState:
    def __init__(self, *, state_dir: Path | None = None, callback_base_url: str = "http://127.0.0.1") -> None:
        self.registry = SessionRegistry()
        self.mcp_router = McpRouter()
        self.process_manager = ProcessManager()
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.shutdown_requested = False
        self.shutdown_in_progress = False
        self.state_dir = state_dir or Path.home() / ".poor-claude"
        self.callback_base_url = callback_base_url


def _bool_metadata(value: str | None) -> bool:
    return value == "True"


def _canonical_session_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def _apply_launch_metadata(session, payload: dict[str, Any]) -> list[str]:
    """Record immutable launch-affecting metadata for a session route.

    Returns a list of warning strings for mismatches that are ignored (not fatal).
    """
    _VALID_PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
    incoming_settings = payload.get("settings_path")
    incoming_settings = incoming_settings if isinstance(incoming_settings, str) else ""
    # --dangerously-skip-permissions is a legacy alias for --permission-mode bypassPermissions
    incoming_permission_mode = payload.get("permission_mode") or (
        "bypassPermissions" if payload.get("dangerously_skip_permissions") else "default"
    )
    if incoming_permission_mode not in _VALID_PERMISSION_MODES:
        raise RuntimeError(
            f"invalid permission_mode {incoming_permission_mode!r}; "
            f"must be one of: {', '.join(sorted(_VALID_PERMISSION_MODES))}"
        )
    incoming_dev_channels = bool(payload.get("dangerously_load_development_channels", True))
    incoming_auto_trust = bool(payload.get("auto_accept_workspace_trust", False))
    incoming_resume = bool(payload.get("resume_session", False))
    incoming_effort = payload.get("effort") or "medium"
    incoming_model = payload.get("model") or ""
    incoming_append_system_prompt = payload.get("append_system_prompt") or ""
    incoming_system_prompt = payload.get("system_prompt") or ""
    raw_tools = payload.get("tools")
    if isinstance(raw_tools, list):
        incoming_tools = json.dumps(sorted(r for r in raw_tools if isinstance(r, str)))
    else:
        incoming_tools = ""
    raw_add_dirs = payload.get("add_dirs")
    if isinstance(raw_add_dirs, list):
        incoming_add_dirs = json.dumps(sorted(r for r in raw_add_dirs if isinstance(r, str)))
    else:
        incoming_add_dirs = ""
    raw_allowed_tools = payload.get("allowed_tools")
    if isinstance(raw_allowed_tools, list):
        incoming_allowed_tools = json.dumps(sorted(r for r in raw_allowed_tools if isinstance(r, str)))
    else:
        incoming_allowed_tools = ""
    raw_disallowed_tools = payload.get("disallowed_tools")
    if isinstance(raw_disallowed_tools, list):
        incoming_disallowed_tools = json.dumps(sorted(r for r in raw_disallowed_tools if isinstance(r, str)))
    else:
        incoming_disallowed_tools = ""
    if not isinstance(incoming_effort, str):
        incoming_effort = "medium"
    if not incoming_dev_channels:
        raise RuntimeError("poor-claude requires development channels for interactive routing")

    existing_settings = session.metadata.get("settings_path", "")
    existing_permission_mode = session.metadata.get("permission_mode") or "default"
    existing_dev_channels = _bool_metadata(
        session.metadata.get("dangerously_load_development_channels", "True")
    )
    existing_auto_trust = _bool_metadata(session.metadata.get("auto_accept_workspace_trust"))
    existing_effort = session.metadata.get("effort") or "medium"
    existing_model = session.metadata.get("model") or ""
    existing_append_system_prompt = session.metadata.get("append_system_prompt") or ""
    existing_system_prompt = session.metadata.get("system_prompt") or ""
    existing_tools = session.metadata.get("tools") or ""
    existing_add_dirs = session.metadata.get("add_dirs") or ""
    existing_allowed_tools = session.metadata.get("allowed_tools") or ""
    existing_disallowed_tools = session.metadata.get("disallowed_tools") or ""

    if "launch_config_frozen" in session.metadata:
        mismatches = []
        if existing_settings != incoming_settings:
            mismatches.append(f"settings_path existing={existing_settings!r} incoming={incoming_settings!r}")
        if existing_permission_mode != incoming_permission_mode:
            mismatches.append(
                f"permission_mode existing={existing_permission_mode!r} incoming={incoming_permission_mode!r}"
            )
        if existing_dev_channels != incoming_dev_channels:
            mismatches.append(
                "dangerously_load_development_channels "
                f"existing={existing_dev_channels!r} incoming={incoming_dev_channels!r}"
            )
        if mismatches:
            raise RuntimeError(
                "existing session launch config differs from request; stop/recreate session or use matching flags: "
                + "; ".join(mismatches)
            )
        if incoming_resume:
            session.metadata["resume_on_launch"] = "True"
        if existing_auto_trust != incoming_auto_trust:
            session.metadata["auto_accept_workspace_trust"] = str(incoming_auto_trust)
        # allowed_tools / disallowed_tools changes take effect immediately:
        # prepare_launch_spec (called by _prepare_launch_metadata right after this)
        # rewrites the policy file that the hook reads fresh on every tool invocation
        # — no restart needed.
        if existing_allowed_tools != incoming_allowed_tools:
            session.metadata["allowed_tools"] = incoming_allowed_tools
        if existing_disallowed_tools != incoming_disallowed_tools:
            session.metadata["disallowed_tools"] = incoming_disallowed_tools
        # Soft params (effort, model, system_prompt, append_system_prompt) require a
        # process restart so the new values are included in the claude command arguments.
        soft_changed = (
            existing_effort != incoming_effort
            or existing_model != incoming_model
            or existing_tools != incoming_tools
            or existing_add_dirs != incoming_add_dirs
            or existing_system_prompt != incoming_system_prompt
            or existing_append_system_prompt != incoming_append_system_prompt
        )
        if soft_changed:
            session.metadata["effort"] = incoming_effort
            session.metadata["model"] = incoming_model
            session.metadata["tools"] = incoming_tools
            session.metadata["add_dirs"] = incoming_add_dirs
            session.metadata["system_prompt"] = incoming_system_prompt
            session.metadata["append_system_prompt"] = incoming_append_system_prompt
            session.metadata["restart_needed"] = "True"
            # CRITICAL: new process must use --resume so conversation history is preserved
            session.metadata["resume_on_launch"] = "True"
        return []

    session.metadata["resume_on_launch"] = str(incoming_resume)
    session.metadata["settings_path"] = incoming_settings
    session.metadata["permission_mode"] = incoming_permission_mode
    session.metadata["dangerously_load_development_channels"] = str(incoming_dev_channels)
    session.metadata["auto_accept_workspace_trust"] = str(incoming_auto_trust)
    session.metadata["effort"] = incoming_effort
    session.metadata["model"] = incoming_model
    session.metadata["tools"] = incoming_tools
    session.metadata["add_dirs"] = incoming_add_dirs
    session.metadata["system_prompt"] = incoming_system_prompt
    session.metadata["append_system_prompt"] = incoming_append_system_prompt
    session.metadata["allowed_tools"] = incoming_allowed_tools
    session.metadata["disallowed_tools"] = incoming_disallowed_tools
    session.metadata["launch_config_frozen"] = "True"
    return []


def _prepare_launch_metadata(state: ControlState, session) -> None:
    if "merged_settings_path" in session.metadata and "launch_command" in session.metadata:
        spec = prepare_launch_spec(
            session=session,
            state_dir=state.state_dir,
            callback_base_url=state.callback_base_url,
        )
        session.metadata["launch_command"] = json.dumps(build_claude_command(spec))
        return
    spec = prepare_launch_spec(
        session=session,
        state_dir=state.state_dir,
        callback_base_url=state.callback_base_url,
    )
    session.metadata["launch_command"] = json.dumps(build_claude_command(spec))


def _ensure_process_metadata(state: ControlState, session) -> None:
    # If soft params changed (effort/model/append_system_prompt), stop the running
    # process first so ensure_running launches a fresh one with the updated params.
    # Conversation history is preserved because resume_on_launch was set to "True".
    if session.metadata.pop("restart_needed", None) == "True":
        state.process_manager.stop(session.route_key)
    spec = prepare_launch_spec(
        session=session,
        state_dir=state.state_dir,
        callback_base_url=state.callback_base_url,
    )
    managed = state.process_manager.ensure_running(route_key=session.route_key, spec=spec)
    session.metadata["process_pid"] = str(managed.process.pid)
    session.metadata["process_alive"] = str(managed.is_alive())


def _prune_sessions_locked(state: ControlState, *, now: float | None = None) -> tuple[list[str], list[tuple[Any, str]], list[str]]:
    current = time.time() if now is None else now
    removed = []
    stopping = []
    cleanup_workdirs = []
    for route, session in list(state.registry._sessions.items()):
        if session.metadata.get("process_stopping") == "True":
            continue
        managed = state.process_manager.get(route)
        if managed is not None:
            session.metadata["process_pid"] = str(managed.process.pid)
            session.metadata["process_alive"] = str(managed.is_alive())
        elif "process_pid" in session.metadata:
            session.metadata["process_alive"] = "False"
        process_dead = session.metadata.get("process_alive") == "False"
        if session.active_request is None and (session.is_idle_expired(current) or process_dead):
            if managed is not None:
                session.metadata["process_stopping"] = "True"
                session.metadata["process_alive"] = "False"
                state.mcp_router.clear_route(route)
                stopping.append((session, route))
            else:
                state.registry._sessions.pop(route, None)
                state.mcp_router.remove_route(route)
                removed.append(route)
                if _should_cleanup_project_settings_locked(state, session.workdir):
                    cleanup_workdirs.append(session.workdir)
    if removed:
        state.condition.notify_all()
    return removed, stopping, cleanup_workdirs


def _prune_sessions(state: ControlState, *, now: float | None = None) -> list[str]:
    with state.lock:
        removed, stopping, cleanup_workdirs = _prune_sessions_locked(state, now=now)
    for session, route in stopping:
        termination_error = None
        try:
            state.process_manager.stop(route)
        except Exception as exc:
            termination_error = exc
        with state.lock:
            if state.registry._sessions.get(route) is not session:
                continue
            if termination_error is None:
                state.registry._sessions.pop(route, None)
                state.mcp_router.remove_route(route)
                removed.append(route)
                if _should_cleanup_project_settings_locked(state, session.workdir):
                    cleanup_workdirs.append(session.workdir)
            else:
                session.metadata["process_stopping"] = "False"
                session.metadata["termination_failed"] = "True"
            state.condition.notify_all()
    with state.lock:
        for workdir in cleanup_workdirs:
            if _should_cleanup_project_settings_locked(state, workdir):
                cleanup_project_local_settings(Path(workdir))
                cleanup_project_mcp_config(Path(workdir))
    return removed


def _diagnostics_for_session(session) -> dict[str, Any]:
    paths = {
        key: value
        for key, value in {
            "stdout": session.metadata.get("claude_stdout_path"),
            "stderr": session.metadata.get("claude_stderr_path"),
            "mcp": session.metadata.get("mcp_log_path"),
        }.items()
        if value
    }
    transcript_paths = transcript_candidates(session_id=session.session_id, workdir=session.workdir)
    existing_transcripts = [str(path) for path in transcript_paths if path.exists()]
    if existing_transcripts:
        paths["transcript"] = existing_transcripts[0]
    return {
        "session_id": session.session_id,
        "route_key": session.route_key,
        "process_alive": session.metadata.get("process_alive"),
        "process_pid": session.metadata.get("process_pid"),
        "paths": paths,
        "summaries": {
            "stdout": _summarize_log(paths.get("stdout")),
            "stderr": _summarize_log(paths.get("stderr")),
            "mcp": _summarize_log(paths.get("mcp")),
        },
    }


def _summarize_log(path: str | None) -> str | None:
    if not path:
        return None
    log_path = Path(path)
    if not log_path.exists():
        return "not found"
    try:
        text = _read_log_tail(log_path, max_bytes=12000)
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc.__class__.__name__}"
    plain = " ".join(text.replace("\x1b", " ").lower().split())
    if not plain:
        return "empty"
    labels = []
    checks = [
        ("bypass permissions startup prompt", "bypass permissions mode"),
        ("development channels startup prompt", "loading development channels"),
        ("auto-accepted startup prompt", "poor-claude auto-accept startup prompt"),
        ("listening for channel messages", "listening for channel messages"),
        ("mcp initialized", '"method": "initialize"'),
        ("channel notification sent", "notifications/claude/channel"),
        ("no conversation found", "no conversation found"),
        ("session id already in use", "session id"),
        ("contains error marker", "error:"),
    ]
    for label, marker in checks:
        if marker in plain:
            labels.append(label)
    return ", ".join(labels) if labels else "has output"


def _read_log_tail(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def _should_cleanup_project_settings_locked(state: ControlState, workdir: str) -> bool:
    return all(session.workdir != workdir for session in state.registry._sessions.values())


def _wait_for_response(state: ControlState, session, request, *, timeout_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    candidates = transcript_candidates(session_id=session.session_id, workdir=session.workdir)
    # Snapshot current transcript sizes so we seek directly to the right position
    # rather than relying on the 1 MB tail-read, which fails for long sessions.
    transcript_offsets: dict[str, int] = {}
    for candidate in candidates:
        try:
            transcript_offsets[str(candidate)] = candidate.stat().st_size
        except OSError:
            transcript_offsets[str(candidate)] = 0
    transcript_quiet_seconds = 0.5
    stable_transcript_response = None
    stable_transcript_signature = None
    stable_transcript_since = 0.0
    transcript_fallback_response = None
    next_candidate_refresh = time.time() + 2.0
    while True:
        with state.lock:
            if state.registry._sessions.get(session.route_key) is not session:
                raise RuntimeError("session route no longer exists")
            active = session.active_request
            if state.shutdown_requested or state.shutdown_in_progress:
                if active is not None and active.request_id == request.request_id:
                    state.registry.timeout_request_for_route(
                        route=session.route_key,
                        request_id=request.request_id,
                    )
                    state.condition.notify_all()
                raise RuntimeError("daemon is shutting down")
            if active is None:
                return request.response or ""

        transcript_response = None
        transcript_stop_reason = None
        transcript_signature = None
        for candidate in candidates:
            transcript_record = read_response_record_after_request_from_file(
                candidate,
                request_id=request.request_id,
                start_offset=transcript_offsets.get(str(candidate), 0),
            )
            if transcript_record is not None:
                transcript_response = transcript_record.text
                transcript_stop_reason = transcript_record.stop_reason
                try:
                    stat = candidate.stat()
                    transcript_signature = (str(candidate), stat.st_size, stat.st_mtime_ns, transcript_response)
                except OSError:
                    transcript_signature = None
                break
        if transcript_response is not None:
            now = time.time()
            if transcript_response != stable_transcript_response or transcript_signature != stable_transcript_signature:
                stable_transcript_response = transcript_response
                stable_transcript_signature = transcript_signature
                stable_transcript_since = now
            elif now - stable_transcript_since < transcript_quiet_seconds:
                pass
            else:
                transcript_fallback_response = transcript_response
                if transcript_stop_reason == "end_turn":
                    with state.lock:
                        if state.registry._sessions.get(session.route_key) is not session:
                            raise RuntimeError("session route no longer exists")
                        state.registry.finish_request_for_route(
                            route=session.route_key,
                            request_id=request.request_id,
                            response=transcript_response,
                        )
                        state.condition.notify_all()
                    return transcript_response
        elif time.time() >= next_candidate_refresh:
            refreshed_candidates = transcript_candidates(session_id=session.session_id, workdir=session.workdir)
            for candidate in refreshed_candidates:
                if candidate not in candidates:
                    candidates.append(candidate)
                    try:
                        transcript_offsets[str(candidate)] = candidate.stat().st_size
                    except OSError:
                        transcript_offsets[str(candidate)] = 0
            next_candidate_refresh = time.time() + 2.0

        remaining = deadline - time.time()
        if remaining <= 0:
            should_stop_process = False
            response_from_transcript = transcript_fallback_response
            with state.lock:
                if state.registry._sessions.get(session.route_key) is not session:
                    raise RuntimeError("session route no longer exists")
                active = session.active_request
                if active is None:
                    return request.response or ""
                if active is not None and active.request_id == request.request_id:
                    if response_from_transcript is None:
                        state.registry.timeout_request_for_route(
                            route=session.route_key,
                            request_id=request.request_id,
                        )
                    else:
                        state.registry.finish_request_for_route(
                            route=session.route_key,
                            request_id=request.request_id,
                            response=response_from_transcript,
                    )
                    session.metadata["process_stopping"] = "True"
                    session.metadata["process_alive"] = "False"
                    should_stop_process = True
                    state.mcp_router.clear_route(session.route_key)
                    state.condition.notify_all()
            if should_stop_process:
                termination_error = None
                try:
                    state.process_manager.stop(session.route_key)
                except Exception as exc:
                    termination_error = exc
                finally:
                    with state.lock:
                        if state.registry._sessions.get(session.route_key) is session:
                            if termination_error is None:
                                session.metadata["process_stopping"] = "False"
                            else:
                                session.metadata["process_stopping"] = "False"
                                session.metadata["termination_failed"] = "True"
                            state.condition.notify_all()
                if termination_error is not None:
                    if response_from_transcript is not None:
                        return response_from_transcript
                    raise RuntimeError(f"timed out waiting for Claude response; failed to stop process: {termination_error}")
            else:
                with state.lock:
                    if state.registry._sessions.get(session.route_key) is session:
                        session.metadata["process_stopping"] = "False"
                        state.condition.notify_all()
            if response_from_transcript is not None:
                return response_from_transcript
            raise TimeoutError("timed out waiting for Claude response")

        with state.condition:
            state.condition.wait(timeout=min(remaining, 0.5))


def make_handler(state: ControlState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "poor-claude-control/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            length = max(0, min(length, 64 * 1024 * 1024))  # cap at 64 MB
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str, diagnostics: dict[str, Any] | None = None) -> None:
            payload = {"error": message}
            if diagnostics is not None:
                payload["diagnostics"] = diagnostics
            self._send_json(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/mcp/next":
                route_key = parse_qs(parsed.query).get("route_key", [""])[0]
                with state.lock:
                    session = state.registry._sessions.get(route_key)
                    queue = state.mcp_router.queue_for(route_key)
                    if session is not None and session.metadata.get("process_stopping") == "True":
                        notification = None
                    else:
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
                        if session.metadata.get("process_stopping") == "True":
                            continue
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
            if self.path != "/shutdown" and (state.shutdown_requested or state.shutdown_in_progress):
                self._send_error(503, "daemon is shutting down")
                return
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
                with state.lock:
                    state.shutdown_in_progress = True
                    state.condition.notify_all()
                stop_error = _stop_all_best_effort(state)
                if stop_error is not None:
                    with state.lock:
                        state.shutdown_in_progress = False
                        state.condition.notify_all()
                    self._send_error(500, f"failed to stop all processes: {stop_error}")
                    return
                with state.lock:
                    state.shutdown_requested = True
                    state.shutdown_in_progress = False
                    state.condition.notify_all()
                response = {"ok": True}
                self._send_json(200, response)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/prune":
                removed = _prune_sessions(state)
                self._send_json(200, {"ok": True, "removed_routes": removed})
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
                    if state.shutdown_requested or state.shutdown_in_progress:
                        raise RuntimeError("daemon is shutting down")
                    session = state.registry.create_or_get(
                        session_id=_canonical_session_id(session_id),
                        ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                        keep_alive=keep_alive,
                        workdir=workdir,
                    )
                    if session.metadata.get("process_stopping") == "True":
                        raise RuntimeError("session process is stopping; retry request")
                    warnings = _apply_launch_metadata(session, payload)
                    _prepare_launch_metadata(state, session)
                    if bool(payload.get("launch_process", False)):
                        _ensure_process_metadata(state, session)
                    state.mcp_router.ensure_route(session.route_key)
                response: dict[str, Any] = {
                    "session_id": session.session_id,
                    "auto_created": session.auto_created,
                    "ttl_seconds": session.ttl_seconds,
                    "keep_alive": session.keep_alive,
                }
                if warnings:
                    response["warnings"] = warnings
                self._send_json(201, response)
            except Exception as exc:
                self._send_error(400, str(exc))

        def do_DELETE(self) -> None:  # noqa: N802
            prefix = "/sessions/"
            if not self.path.startswith(prefix):
                self._send_error(404, "not found")
                return
            session_id = _canonical_session_id(self.path[len(prefix) :])
            if session_id is None:
                self._send_error(404, "session not found")
                return
            workdir = self.headers.get("X-Poor-Claude-Workdir", os.getcwd())
            should_stop_process = False
            with state.lock:
                existing = state.registry.get(session_id, workdir=workdir)
                if existing is not None:
                    if existing.metadata.get("process_stopping") == "True":
                        self._send_error(400, "session process is stopping; retry request")
                        return
                    existing.metadata["process_stopping"] = "True"
                    state.mcp_router.clear_route(existing.route_key)
                    should_stop_process = True
            if existing is None:
                self._send_error(404, "session not found")
                return
            if should_stop_process:
                try:
                    state.process_manager.stop(existing.route_key)
                except Exception as exc:
                    with state.lock:
                        if state.registry._sessions.get(existing.route_key) is existing:
                            existing.metadata["process_stopping"] = "False"
                            existing.metadata["termination_failed"] = "True"
                            state.condition.notify_all()
                    self._send_error(400, f"failed to stop process: {exc}")
                    return
            with state.lock:
                state.registry._sessions.pop(existing.route_key, None)
                state.mcp_router.remove_route(existing.route_key)
                if _should_cleanup_project_settings_locked(state, existing.workdir):
                    cleanup_project_local_settings(Path(existing.workdir))
                    cleanup_project_mcp_config(Path(existing.workdir))
                state.condition.notify_all()
            self._send_json(200, {"ok": True, "session_id": session_id})

        def _handle_request(self) -> None:
            session = None
            try:
                payload = self._read_json()
                session_id = payload.get("session_id")
                prompt = payload.get("prompt")
                if prompt is None:
                    raise RuntimeError("missing required field: prompt")
                timeout_seconds = int(payload.get("timeout_seconds", 300))
                wait_for_response = bool(payload.get("wait_for_response", False))
                ttl_seconds = payload.get("ttl_seconds")
                keep_alive = bool(payload.get("keep_alive", False))
                workdir = str(payload.get("workdir", os.getcwd()))
                with state.lock:
                    if state.shutdown_requested or state.shutdown_in_progress:
                        raise RuntimeError("daemon is shutting down")
                    session = state.registry.create_or_get(
                        session_id=_canonical_session_id(session_id),
                        ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                        keep_alive=keep_alive,
                        workdir=workdir,
                    )
                    if session.metadata.get("process_stopping") == "True":
                        raise RuntimeError("session process is stopping; retry request")
                    warnings = _apply_launch_metadata(session, payload)
                    _prepare_launch_metadata(state, session)
                    if bool(payload.get("launch_process", False)):
                        _ensure_process_metadata(state, session)
                    request = state.registry.start_request_for_route(
                        route=session.route_key,
                        prompt=str(prompt),
                        timeout_seconds=timeout_seconds,
                    )
                    _route_key = session.route_key
                    _session_id_for_prompt = session.session_id
                    _request_id = request.request_id
                # Release the lock before the async MCP call so other threads
                # (e.g. the Stop hook handler) are not blocked while we wait.
                notification = asyncio.run(
                    state.mcp_router.route_prompt(
                        route_key=_route_key,
                        session_id=_session_id_for_prompt,
                        request_id=_request_id,
                        prompt=str(prompt),
                    )
                )
                if wait_for_response:
                    response = _wait_for_response(state, session, request, timeout_seconds=timeout_seconds)
                    resp_body: dict[str, Any] = {
                        "request_id": request.request_id,
                        "session_id": session.session_id,
                        "route_key": session.route_key,
                        "status": "completed",
                        "response": response or "",
                    }
                    if warnings:
                        resp_body["warnings"] = warnings
                    self._send_json(200, resp_body)
                    return
                queued_body: dict[str, Any] = {
                    "request_id": request.request_id,
                    "session_id": session.session_id,
                    "route_key": session.route_key,
                    "status": "queued",
                    "channel_notification": notification.json_rpc(),
                }
                if warnings:
                    queued_body["warnings"] = warnings
                self._send_json(202, queued_body)
            except Exception as exc:
                diagnostics = _diagnostics_for_session(session) if session is not None else None
                self._send_error(400, str(exc), diagnostics=diagnostics)

        def _handle_stop_hook(self) -> None:
            try:
                payload = self._read_json()
                session_id = _canonical_session_id(payload["session_id"])
                if session_id is None:
                    raise RuntimeError("Stop hook payload missing session_id")
                request_id = payload.get("request_id")
                response = str(payload.get("response", ""))
                workdir = str(payload.get("cwd") or payload.get("workdir") or os.getcwd())
                with state.lock:
                    session = state.registry.get(session_id, workdir=workdir)
                    if session is None:
                        raise RuntimeError("session route not found for Stop hook")
                    if not isinstance(request_id, str):
                        raise RuntimeError("Stop hook payload missing request_id")
                    if request_id in session.completed_request_ids:
                        self._send_json(200, {"ok": True, "duplicate": True})
                        return
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
    server.daemon_threads = False
    server.timeout = 0.5
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    write_state(state_file, DaemonState(pid=os.getpid(), address=address))

    def remove_state(*_: object) -> None:
        state_file.unlink(missing_ok=True)

    shutdown_signal = threading.Event()

    def request_shutdown(*_: object) -> None:
        shutdown_signal.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    next_prune = time.time() + 60.0
    try:
        while not state.shutdown_requested:
            server.handle_request()
            if shutdown_signal.is_set():
                with state.lock:
                    state.shutdown_in_progress = True
                    state.shutdown_requested = True
                    state.condition.notify_all()
            if time.time() >= next_prune:
                _prune_sessions(state)
                next_prune = time.time() + 60.0
    finally:
        with state.lock:
            cleanup_workdirs = sorted({session.workdir for session in state.registry._sessions.values()})
        first_stop_error = _stop_all_best_effort(state)
        server_close_error = None
        try:
            server.server_close()
        except Exception as exc:
            server_close_error = exc
        second_stop_error = _stop_all_best_effort(state)
        if first_stop_error is None and second_stop_error is None:
            with state.lock:
                for workdir in cleanup_workdirs:
                    cleanup_project_local_settings(Path(workdir))
                    cleanup_project_mcp_config(Path(workdir))
            remove_state()
        if server_close_error is not None:
            raise server_close_error
    return 0


def _stop_all_best_effort(state: ControlState) -> Exception | None:
    try:
        state.process_manager.stop_all()
    except Exception as exc:
        return exc
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    return serve(state_file=Path(args.state_file), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Local control daemon for poor-claude."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
import uuid
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from poor_claude.daemon import DaemonState, write_state
from poor_claude.launcher import build_claude_command, cleanup_project_mcp_config, prepare_launch_spec
from poor_claude.mcp_router import McpRouter
from poor_claude.process_manager import ProcessManager
from poor_claude.settings import cleanup_project_local_settings
from poor_claude.session import SessionRegistry, TooManyRequestsError
from poor_claude.transcript import (
    find_background_agent_ids_in_transcript,
    find_background_task_ids_in_transcript,
    find_completed_task_ids_in_transcript,
    read_response_record_after_request_from_file,
    transcript_candidates,
)


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
            # Defensive cancel: the queue invariant guarantees pending_request_queue
            # is empty when active_request is None, but guard against future invariant
            # breaks so waiting handler threads are never silently orphaned.
            # Notify coverage: queued handler threads wait on activation_event, not
            # the condition variable, so they do not need condition.notify_all() here.
            # The notify at the end of _prune_sessions_locked (for removed routes) and
            # the stop() finally block (for stopping routes) cover any _wait_for_response
            # waiters on the condition.
            state.registry.cancel_queued_requests_for_route(route=route)
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


def _get_process_exit_reason(stdout_path: str | None) -> str:
    """Read the tail of Claude's stdout log to determine the cause of exit.

    Returns a human-readable message suitable for returning to the caller.
    Reads only the last 8 KiB so it is cheap regardless of log size.
    """
    _default = (
        "Claude process exited unexpectedly; "
        "session may be resumable with --resume"
    )
    if not stdout_path:
        return _default
    try:
        path = Path(stdout_path)
        size = path.stat().st_size
        if size == 0:
            return _default
        with path.open("rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read()
        tail_lower = tail.lower()
        if b"/rate-limit-options" in tail_lower or b"stop and wait for limit to reset" in tail_lower:
            return (
                "Org monthly spend limit reached — Claude has stopped and saved the session. "
                "It will resume automatically when the limit resets."
            )
    except OSError:
        pass
    return _default


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
    # Set to True after we scan the transcript for background agents once per
    # stable-response window; reset whenever the response changes so we rescan
    # when a new end_turn response appears (e.g. after Claude resumes following
    # a SubagentStop task-notification).
    transcript_bg_agents_scanned = False
    # Set to True when we discover ANY background work (Agent or Bash task).
    # Prevents _wait_for_response from finishing with a premature "LAUNCHED"
    # response when all discovered work already completed before our transcript
    # scan (SubagentStop / task-notification arrived first).  In that case
    # pending_background_agent_ids is empty but we still need to wait for the
    # second Stop hook to fire with the real final response.
    bg_work_detected = False
    # --- no-response watchdog -------------------------------------------------
    # Claude Code can hang mid-turn waiting on an SSE stream from Anthropic that
    # never delivers (observed any time during a session, not just at startup;
    # see anthropics/claude-code#26224, #57103).  When that happens Claude writes
    # nothing more to the transcript and never fires its Stop hook, so this loop
    # would otherwise sit idle until the hard timeout and kill the process.
    #
    # The reported workaround is that sending another message (even a single
    # character) restarts the stalled SSE connection.  So before giving up we
    # detect a stall (no transcript growth for POOR_CLAUDE_STALL_SECONDS) and
    # send up to POOR_CLAUDE_MAX_NUDGES nudge notifications to wake Claude up.
    # If nudging doesn't help, the existing deadline/kill path still backstops.
    #
    # NOTE: stall detection keys off transcript byte growth, which cannot tell a
    # genuine hang apart from a legitimately long quiet *foreground* tool call —
    # a slow `Bash` test/build/install that writes nothing until it returns looks
    # identical to a hang.  If the stall window is shorter than such a call the
    # watchdog will inject a nudge into a live, healthy turn.  (Background agents
    # are guarded against below via pending_background_agent_ids; foreground waits
    # have no such signal.)
    #
    # Because the nudge's *efficacy* (does a channel notification actually wake a
    # Claude hung on SSE?) is Claude-Code-internal behaviour we cannot verify from
    # here, the watchdog ships **disabled by default** (POOR_CLAUDE_STALL_SECONDS=0).
    # Enable it only after validating on a box where the hang reproduces, and set
    # the stall window above your agents' longest expected quiet period.  The
    # window must also be < the request timeout, or the kill path fires first.
    try:
        stall_seconds = float(os.environ.get("POOR_CLAUDE_STALL_SECONDS", "0"))
    except ValueError:
        print(
            f"poor-claude: WARNING: invalid POOR_CLAUDE_STALL_SECONDS="
            f"{os.environ['POOR_CLAUDE_STALL_SECONDS']!r}; watchdog disabled",
            file=sys.stderr,
        )
        stall_seconds = 0.0
    # What to do on a detected stall:
    #   off                 - nothing (rely on the hard timeout/kill path)
    #   nudge               - send nudge messages only
    #   restart             - kill + `claude --resume` + re-inject, no nudge
    #   nudge_then_restart  - nudge up to MAX_NUDGES, then escalate to one restart
    # Nudging is cheap and preserves the turn but may be inert if Claude's loop is
    # fully wedged; restart is heavier but almost always recovers.  The ladder
    # covers both a soft SSE stall and a hard process wedge with one config.
    _VALID_STALL_ACTIONS = {"off", "nudge", "restart", "nudge_then_restart"}
    stall_action = os.environ.get("POOR_CLAUDE_STALL_ACTION", "nudge_then_restart")
    if stall_action not in _VALID_STALL_ACTIONS:
        print(
            f"poor-claude: WARNING: invalid POOR_CLAUDE_STALL_ACTION={stall_action!r}; "
            f"watchdog disabled (valid: {', '.join(sorted(_VALID_STALL_ACTIONS))})",
            file=sys.stderr,
        )
        stall_action = "off"
    try:
        max_nudges = int(os.environ.get("POOR_CLAUDE_MAX_NUDGES", "3"))
    except ValueError:
        print(
            f"poor-claude: WARNING: invalid POOR_CLAUDE_MAX_NUDGES="
            f"{os.environ['POOR_CLAUDE_MAX_NUDGES']!r}; using default 3",
            file=sys.stderr,
        )
        max_nudges = 3
    try:
        max_restarts = int(os.environ.get("POOR_CLAUDE_MAX_RESTARTS", "1"))
    except ValueError:
        print(
            f"poor-claude: WARNING: invalid POOR_CLAUDE_MAX_RESTARTS="
            f"{os.environ['POOR_CLAUDE_MAX_RESTARTS']!r}; using default 1",
            file=sys.stderr,
        )
        max_restarts = 1
    nudge_prompt = os.environ.get(
        "POOR_CLAUDE_NUDGE_PROMPT",
        "Please continue your previous response.",
    )
    last_activity_at = time.time()
    last_total_size = sum(transcript_offsets.values())
    nudges_sent = 0
    restarts_done = 0
    # --------------------------------------------------------------------------
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
            # Use request.response is not None as the primary completion signal.
            # request is a direct reference to the PendingRequest object, so this
            # check cannot be "evicted" the way completed_request_ids (maxlen=256)
            # could be in a long-lived session with hundreds of completed requests.
            # completed_request_ids is kept as a belt-and-suspenders fallback only.
            if request.response is not None or request.request_id in session.completed_request_ids:
                final = request.response or ""
                intermediate = request.intermediate_response
                if intermediate and intermediate != final:
                    return f"{intermediate}\n\n{final}"
                return final

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
                # New content → rescan for background agents on next stable window.
                transcript_bg_agents_scanned = False
            elif now - stable_transcript_since < transcript_quiet_seconds:
                pass
            else:
                transcript_fallback_response = transcript_response
                if transcript_stop_reason == "end_turn":
                    # Scan the transcript for background agents/tasks not registered via
                    # hooks (PostToolUse doesn't fire in bypassPermissions mode; Bash tasks
                    # have no SubagentStop equivalent).
                    # Only scan once per stable-response window to avoid repeated I/O.
                    if not transcript_bg_agents_scanned:
                        transcript_bg_agents_scanned = True
                        newly_discovered: list[str] = []
                        newly_discovered_tasks: list[str] = []
                        completed_tasks: list[str] = []
                        for candidate in candidates:
                            found_agents = find_background_agent_ids_in_transcript(
                                candidate,
                                request_id=request.request_id,
                                start_offset=transcript_offsets.get(str(candidate), 0),
                            )
                            found_tasks = find_background_task_ids_in_transcript(
                                candidate,
                                request_id=request.request_id,
                                start_offset=transcript_offsets.get(str(candidate), 0),
                            )
                            found_completed = find_completed_task_ids_in_transcript(
                                candidate,
                                request_id=request.request_id,
                                start_offset=transcript_offsets.get(str(candidate), 0),
                            )
                            # Accumulate completions from every candidate — a stale
                            # task-notification in an earlier candidate must not mask
                            # a launch that only appears in a later candidate.
                            for tid in found_completed:
                                if tid not in completed_tasks:
                                    completed_tasks.append(tid)
                            if found_agents or found_tasks:
                                # Found the candidate that has the launches; stop here.
                                newly_discovered = found_agents
                                newly_discovered_tasks = found_tasks
                                break
                    else:
                        newly_discovered = []
                        newly_discovered_tasks = []
                        completed_tasks = []
                    with state.lock:
                        if state.registry._sessions.get(session.route_key) is not session:
                            raise RuntimeError("session route no longer exists")
                        # Register newly discovered background agents (SubagentStop may
                        # fire before we scan, so guard against re-adding completed ones).
                        for aid in newly_discovered:
                            if aid not in session.completed_agent_ids and aid not in session.pending_background_agent_ids:
                                session.pending_background_agent_ids.add(aid)
                        # Register newly discovered Bash background tasks.
                        for tid in newly_discovered_tasks:
                            if tid not in session.completed_agent_ids and tid not in session.pending_background_agent_ids:
                                session.pending_background_agent_ids.add(tid)
                        if newly_discovered or newly_discovered_tasks:
                            bg_work_detected = True
                            # First-one-wins: store the premature transcript
                            # response as the intermediate output so it can be
                            # prepended to the final response.  Guard against
                            # the Stop hook path having already set this.
                            if request.intermediate_response is None and stable_transcript_response:
                                request.intermediate_response = stable_transcript_response
                        # Remove Bash tasks that reached a terminal state.  Unlike agent
                        # tasks (cleared by SubagentStop), completion is transcript-only.
                        for tid in completed_tasks:
                            session.completed_agent_ids.add(tid)
                            session.pending_background_agent_ids.discard(tid)
                        if len(session.pending_background_agent_ids) > 0:
                            # Background agents are still running; the transcript
                            # shows the premature "launched" turn.  Reset the
                            # stability timer AND clear the stable-response bookmarks
                            # so the next polling cycle must observe a genuinely new,
                            # stable response before completing.
                            # Without clearing stable_transcript_response /
                            # _signature, a SubagentStop that fires while the set
                            # empties and the very next loop iteration would
                            # immediately complete with this stale response (same
                            # signature → timer already expired).
                            stable_transcript_since = time.time()
                            stable_transcript_response = None  # force re-stabilisation
                            stable_transcript_signature = None
                            transcript_bg_agents_scanned = False  # Rescan on next stable response
                        elif bg_work_detected:
                            # All discovered background work already completed before
                            # our transcript scan (SubagentStop / task-notification
                            # arrived first, landing IDs in completed_agent_ids).
                            # Do NOT finish here — the second Stop hook will fire
                            # after Claude resumes and outputs the real final response,
                            # and that Stop handler calls finish_request_for_route with
                            # the correct content.  Just reset the stability timer so
                            # we keep looping efficiently until active_request → None.
                            stable_transcript_since = time.time()
                        else:
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

        # --- no-response watchdog: recover a stalled Claude before timing out ---
        if stall_seconds > 0 and stall_action != "off":
            current_total_size = 0
            for candidate in candidates:
                try:
                    current_total_size += candidate.stat().st_size
                except OSError:
                    pass
            if current_total_size != last_total_size:
                # Transcript grew → Claude is making progress; reset the watchdog.
                # (Nudge budget refills for a fresh stall; restart budget does not,
                # so a wedged session can't trigger an unbounded restart loop.)
                last_total_size = current_total_size
                last_activity_at = time.time()
                if nudges_sent != 0:
                    nudges_sent = 0
                    # M4: sync the reset back to metadata so status/observers see 0.
                    with state.lock:
                        session.metadata["nudges_sent"] = "0"
            elif (
                request.response is None
                and not session.pending_background_agent_ids
                and time.time() - last_activity_at >= stall_seconds
            ):
                # No transcript growth for the whole stall window, the request is
                # still open, and no background agent/task is in flight: Claude is
                # likely hung (a stalled SSE stream — anthropics/claude-code#26224)
                # rather than legitimately waiting on background work (whose quiet
                # main transcript would otherwise look identical to a hang).
                want_nudge = stall_action in ("nudge", "nudge_then_restart") and nudges_sent < max_nudges
                want_restart = stall_action in ("restart", "nudge_then_restart") and restarts_done < max_restarts
                if want_nudge:
                    # Cheap first: a message reportedly revives a stuck SSE stream.
                    nudges_sent += 1
                    last_activity_at = time.time()  # wait another window before re-nudging
                    skip_nudge = False
                    with state.lock:
                        # Re-check under lock — the request may have completed between
                        # the unlocked stall check above and acquiring this lock.
                        if request.response is not None or session.active_request is not request:
                            nudges_sent -= 1  # undo the increment; request is already done
                            skip_nudge = True
                        else:
                            session.metadata["nudges_sent"] = str(nudges_sent)
                            session.metadata["last_nudge_at"] = str(time.time())
                    if not skip_nudge:
                        try:
                            asyncio.run(
                                state.mcp_router.route_prompt(
                                    route_key=session.route_key,
                                    session_id=session.session_id,
                                    request_id=request.request_id,
                                    prompt=nudge_prompt,
                                )
                            )
                        except Exception:  # noqa: BLE001 - best-effort; restart/kill backstops
                            pass
                elif want_restart:
                    # Nudging exhausted or disabled and the process is still wedged:
                    # kill it and relaunch with `--resume` (conversation preserved via
                    # resume_on_launch -> spec.resume), then re-inject the stuck prompt
                    # so the resumed session processes it.  Reuses the tested
                    # _ensure_process_metadata relaunch path.
                    last_activity_at = time.time()
                    nudges_sent = 0
                    do_restart = False
                    with state.lock:
                        if state.registry._sessions.get(session.route_key) is not session:
                            raise RuntimeError("session route no longer exists")
                        # H2: re-check under lock — the Stop hook may have completed
                        # the request between the unlocked stall check and acquiring
                        # this lock.  The early-return at the top of this loop then
                        # returns the response without a spurious process kill.
                        if request.response is None and session.active_request is request:
                            do_restart = True
                            restarts_done += 1
                            session.metadata["restart_needed"] = "True"
                            session.metadata["resume_on_launch"] = "True"
                            session.metadata["stall_restarts"] = str(restarts_done)
                            session.metadata["last_restart_at"] = str(time.time())
                            session.metadata["nudges_sent"] = "0"  # M4: sync reset
                            state.mcp_router.clear_route(session.route_key)
                            # H1: do NOT call _ensure_process_metadata here — it calls
                            # process_manager.stop() which can block up to ~8 s (SIGTERM
                            # wait + drain), freezing every lock waiter including Stop
                            # hook handlers.  Called below after releasing the lock.
                    if do_restart:
                        # H1: process stop + relaunch outside the lock.  Safe because
                        # the only other _ensure_process_metadata call sites are:
                        # (a) new-session POST — different session object; and
                        # (b) the launch_process path — guarded against running when
                        # active_request is not None (see "restart_needed" guard above).
                        #
                        # Best-effort window: if the Stop hook completes the request
                        # between this point and the process_manager.stop() call inside
                        # _ensure_process_metadata, the finished process is killed and
                        # the re-injected prompt hits the resumed session.  The response
                        # is still returned correctly on the next loop iteration via the
                        # early-return at the top of this loop.  This window is narrow
                        # (lock release → stop()) and the consequence is a spurious
                        # resume, not a lost response.
                        try:
                            _ensure_process_metadata(state, session)
                        except Exception:  # noqa: BLE001 - best-effort; kill path backstops
                            pass
                        # M1: give the resumed session a full timeout window from
                        # now — without this the remaining budget after nudging
                        # (up to MAX_NUDGES × stall_seconds elapsed) may not be
                        # enough for the resumed turn to complete.
                        deadline = max(deadline, time.time() + timeout_seconds)
                        with state.lock:
                            state.condition.notify_all()
                        try:
                            asyncio.run(
                                state.mcp_router.route_prompt(
                                    route_key=session.route_key,
                                    session_id=session.session_id,
                                    request_id=request.request_id,
                                    prompt=str(request.prompt),
                                )
                            )
                        except Exception:  # noqa: BLE001 - best-effort; kill path backstops
                            pass

        # --- fast-fail on process death -------------------------------------------
        # Detect when Claude exits without firing a Stop hook (e.g. after the
        # rate-limit TUI is dismissed via `\r` injection) so we return an error
        # immediately instead of waiting for the hard 30-min timeout.
        #
        # Guard: skip when process_stopping is "True" (the deadline-kill path owns
        # the process lifecycle and will return its own error) or when no process
        # has ever been started for this session.
        #
        # The stall-watchdog restart path is self-synchronised: _ensure_process_metadata
        # runs synchronously on *this same thread* (see line `_ensure_process_metadata(
        # state, session)` in the want_restart block above), so by the time execution
        # reaches here the new process is already registered in process_manager —
        # process_manager.get() returns the live replacement, never None, ruling out
        # false positives from that path.
        if (
            session.metadata.get("process_pid")
            and session.metadata.get("process_stopping") != "True"
        ):
            managed = state.process_manager.get(session.route_key)
            if managed is None:
                exit_msg = _get_process_exit_reason(session.metadata.get("claude_stdout_path"))
                # Prefer a response Claude already wrote to the transcript over the
                # error message — it's possible the process crashed after completing
                # a turn but before the Stop hook fired.
                #
                # Check transcript_response (current iteration) with end_turn guard first:
                # transcript_fallback_response only gets promoted after 0.5 s of stability,
                # so on the first iteration where a complete response appears it is still
                # None even though the answer is already on disk.  A partial tool-use
                # response is NOT a complete turn — prefer exit_msg in that case.
                if transcript_response is not None and transcript_stop_reason == "end_turn":
                    exit_msg = transcript_response
                elif transcript_fallback_response is not None:
                    exit_msg = transcript_fallback_response
                with state.lock:
                    if state.registry._sessions.get(session.route_key) is not session:
                        raise RuntimeError("session route no longer exists")
                    # Re-check under lock: the Stop hook may have fired between
                    # process_manager.get() and acquiring this lock.
                    if request.response is not None or request.request_id in session.completed_request_ids:
                        final = request.response or ""
                        intermediate = request.intermediate_response
                        if intermediate and intermediate != final:
                            return f"{intermediate}\n\n{final}"
                        return final
                    if session.active_request is request:
                        state.registry.finish_request_for_route(
                            route=session.route_key,
                            request_id=request.request_id,
                            response=exit_msg,
                        )
                        session.metadata["process_alive"] = "False"
                        state.condition.notify_all()
                    else:
                        # Defensive: request is no longer active (superseded by another
                        # or the registry was updated between process_manager.get() and
                        # acquiring this lock).  Mark it done so it is not left in a
                        # partial state; the active request will be handled by its own
                        # handler thread.
                        request.response = exit_msg
                        session.completed_request_ids.append(request.request_id)
                        state.condition.notify_all()
                intermediate = request.intermediate_response
                if intermediate and intermediate != exit_msg:
                    return f"{intermediate}\n\n{exit_msg}"
                return exit_msg

        remaining = deadline - time.time()
        if remaining <= 0:
            should_stop_process = False
            response_from_transcript = transcript_fallback_response
            with state.lock:
                if state.registry._sessions.get(session.route_key) is not session:
                    raise RuntimeError("session route no longer exists")
                # If our request completed (e.g. promoted queue brought active_request
                # to a different request), return immediately instead of timing out.
                # Primary: request.response is not None (eviction-safe object field).
                if request.response is not None or request.request_id in session.completed_request_ids:
                    final = request.response or ""
                    intermediate = request.intermediate_response
                    if intermediate and intermediate != final:
                        return f"{intermediate}\n\n{final}"
                    return final
                active = session.active_request
                if active is not None and active.request_id == request.request_id:
                    if response_from_transcript is None:
                        state.registry.timeout_request_for_route(
                            route=session.route_key,
                            request_id=request.request_id,
                        )
                    else:
                        # The process is about to be killed; finish R1 and cancel the
                        # queue atomically so queued handlers wake up with an error
                        # rather than calling route_prompt against a dead route.
                        state.registry.finish_and_cancel_queue_for_route(
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
                        intermediate = request.intermediate_response
                        if intermediate and intermediate != response_from_transcript:
                            return f"{intermediate}\n\n{response_from_transcript}"
                        return response_from_transcript
                    raise RuntimeError(f"timed out waiting for Claude response; failed to stop process: {termination_error}")
            else:
                with state.lock:
                    if state.registry._sessions.get(session.route_key) is session:
                        session.metadata["process_stopping"] = "False"
                        state.condition.notify_all()
            if response_from_transcript is not None:
                intermediate = request.intermediate_response
                if intermediate and intermediate != response_from_transcript:
                    return f"{intermediate}\n\n{response_from_transcript}"
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
                            "pending_queue_depth": len(session.pending_request_queue),
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
            if self.path == "/hook/agent-launched":
                self._handle_agent_launched_hook()
                return
            if self.path == "/hook/subagent-stop":
                self._handle_subagent_stop_hook()
                return
            if self.path == "/hook/rate-limit":
                self._handle_rate_limit_hook()
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
                    # Wake handler threads blocked on activation_event.wait() so
                    # they can observe shutdown_requested and return an error.
                    for session in state.registry._sessions.values():
                        state.registry.cancel_queued_requests_for_route(route=session.route_key)
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
                # Cancel queued requests before removing the session so handler
                # threads waiting on activation_event.wait() receive a cancellation
                # signal and can return an error to their callers.
                state.registry.cancel_queued_requests_for_route(route=existing.route_key)
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
                        # Guard: do not restart the process while another request is
                        # active — that would kill R1 mid-flight.  Leave restart_needed
                        # in metadata so the restart happens on the next launch_process=True
                        # call after the session becomes idle.
                        if (
                            session.metadata.get("restart_needed") == "True"
                            and (session.active_request is not None or session.pending_request_queue)
                        ):
                            raise RuntimeError(
                                "session is busy; cannot restart process with updated params "
                                "while a request is active - retry after the current request completes"
                            )
                        _ensure_process_metadata(state, session)
                    # Reject fire-and-forget requests when the session is busy:
                    # the caller expects an immediate 202, not a silent queue wait.
                    if not wait_for_response and session.active_request is not None:
                        raise RuntimeError(
                            "session is busy; fire-and-forget requests cannot be queued "
                            "- use wait_for_response=True or retry after the active request completes"
                        )
                    _too_many_error: str | None = None
                    try:
                        request, is_active = state.registry.enqueue_or_activate_request_for_route(
                            route=session.route_key,
                            prompt=str(prompt),
                            timeout_seconds=timeout_seconds,
                        )
                    except TooManyRequestsError as exc:
                        # Capture the message; send the 429 OUTSIDE the lock to avoid
                        # blocking other handler threads during the socket write.
                        _too_many_error = str(exc)
                    else:
                        _route_key = session.route_key
                        _session_id_for_prompt = session.session_id
                        _request_id = request.request_id

                # Send 429 outside the lock so other threads are not blocked.
                if _too_many_error is not None:
                    self._send_error(429, _too_many_error)
                    return

                if not is_active:
                    # Another request is running — block until this one is promoted.
                    # Poll with a bounded wait so we detect process death and daemon
                    # shutdown even when activation_event is never explicitly set.
                    # Timeout does NOT start here; it starts after activation, so
                    # timeout_seconds measures only Claude's processing time.
                    while not request.activation_event.wait(timeout=5.0):
                        with state.lock:
                            if state.shutdown_requested or state.shutdown_in_progress:
                                state.registry.cancel_queued_requests_for_route(route=_route_key)
                                break
                            session_now = state.registry._sessions.get(_route_key)
                            if session_now is None:
                                break  # session removed; request.cancelled already set
                            if session_now.metadata.get("process_alive") == "False":
                                # Process died while we were waiting; wake everyone at once.
                                state.registry.cancel_queued_requests_for_route(route=_route_key)
                                break
                    if request.cancelled:
                        with state.lock:
                            is_shutdown = state.shutdown_requested or state.shutdown_in_progress
                        if is_shutdown:
                            self._send_error(503, "daemon is shutting down; queued request cancelled")
                            return
                        raise RuntimeError("queued request was cancelled: session was stopped or process died")
                    with state.lock:
                        if state.shutdown_requested or state.shutdown_in_progress:
                            self._send_error(503, "daemon is shutting down")
                            return
                        if state.registry._sessions.get(_route_key) is None:
                            raise RuntimeError("session was stopped while request was queued")

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
                    # Pass request.timeout_seconds so the deadline starts from
                    # activation time (after route_prompt), not from enqueue time.
                    response = _wait_for_response(state, session, request, timeout_seconds=request.timeout_seconds)
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
                transcript_path_str = payload.get("transcript_path")
                # Scan the transcript for background agents that were not registered
                # via PostToolUse (PostToolUse doesn't fire in bypassPermissions mode).
                # This must happen BEFORE acquiring the lock so we don't hold the lock
                # during file I/O, and BEFORE the completed_request_ids check so we
                # can populate pending_background_agent_ids even on the first Stop.
                discovered_agents: list[str] = []
                discovered_tasks: list[str] = []
                completed_tasks: list[str] = []
                if isinstance(transcript_path_str, str) and isinstance(request_id, str):
                    try:
                        # All three calls use the default start_offset=0, which
                        # triggers a 1 MB tail-read.  The Stop hook payload does
                        # not carry a byte offset, and poor-claude does not yet
                        # record the transcript file size at request-start time,
                        # so we cannot seek directly.  For sessions whose transcript
                        # exceeds 1 MB before the current request begins, the
                        # launch event may lie outside the tail window; the
                        # request_id marker scoping prevents cross-request confusion
                        # but will silently return [] if the marker is out of range.
                        # TODO: extend the hook payload or PendingRequest to carry
                        # a start_offset so we can seek past earlier requests.
                        discovered_agents = find_background_agent_ids_in_transcript(
                            Path(transcript_path_str), request_id=request_id
                        )
                        discovered_tasks = find_background_task_ids_in_transcript(
                            Path(transcript_path_str), request_id=request_id
                        )
                        completed_tasks = find_completed_task_ids_in_transcript(
                            Path(transcript_path_str), request_id=request_id
                        )
                    except Exception:  # noqa: BLE001
                        pass  # best-effort; transcript may not be available yet
                with state.lock:
                    session = state.registry.get(session_id, workdir=workdir)
                    if session is None:
                        raise RuntimeError("session route not found for Stop hook")
                    if not isinstance(request_id, str):
                        # The stop hook payload does not carry a poor-claude request_id
                        # (Claude Code has no knowledge of it).  Fall back to the
                        # session's active request — the Stop hook fires after Claude
                        # finishes responding, so the active request IS the one we want
                        # to complete.
                        if session.active_request is not None:
                            request_id = session.active_request.request_id
                        else:
                            # No active request — either already completed (duplicate
                            # hook invocation) or the request was cancelled/timed out.
                            self._send_json(200, {"ok": True, "no_active_request": True})
                            return
                    if request_id in session.completed_request_ids:
                        self._send_json(200, {"ok": True, "duplicate": True})
                        return
                    # Register newly discovered background agents that haven't
                    # already completed (SubagentStop may fire before this Stop hook).
                    for aid in discovered_agents:
                        if aid not in session.completed_agent_ids and aid not in session.pending_background_agent_ids:
                            session.pending_background_agent_ids.add(aid)
                    # Register newly discovered Bash background tasks.
                    for tid in discovered_tasks:
                        if tid not in session.completed_agent_ids and tid not in session.pending_background_agent_ids:
                            session.pending_background_agent_ids.add(tid)
                    # Remove Bash tasks that have reached a terminal state
                    # (completed, killed, failed, stopped).  Unlike agent tasks,
                    # there is no SubagentStop hook — completion is detected
                    # entirely from the transcript.
                    for tid in completed_tasks:
                        session.completed_agent_ids.add(tid)
                        session.pending_background_agent_ids.discard(tid)
                    if len(session.pending_background_agent_ids) > 0:
                        # Background agents are still running.  Defer completion
                        # until SubagentStop fires for each of them and the set
                        # empties.  The final Stop (after Claude resumes and
                        # writes the real response) will complete the request.
                        # Save the current (premature) response as the intermediate
                        # output so it can be prepended to the final response later.
                        active_req = session.active_request
                        if active_req is not None and active_req.intermediate_response is None and response:
                            active_req.intermediate_response = response
                        self._send_json(200, {"ok": True, "deferred": True})
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

        def _handle_agent_launched_hook(self) -> None:
            """Register a background-agent agentId for a session.

            Called by the PostToolUse hook when it sees a background Agent response
            (isAsync: true).  The agentId is added to the session's
            pending_background_agent_ids set so the Stop hook knows to defer until
            this agent's SubagentStop fires.
            """
            try:
                payload = self._read_json()
                session_id = _canonical_session_id(payload.get("session_id"))
                if session_id is None:
                    raise RuntimeError("agent-launched payload missing session_id")
                agent_id = payload.get("agent_id")
                if not isinstance(agent_id, str) or not agent_id:
                    raise RuntimeError("agent-launched payload missing agent_id")
                workdir = str(payload.get("cwd") or os.getcwd())
                with state.lock:
                    session = state.registry.get(session_id, workdir=workdir)
                    if session is None:
                        # Session may not exist yet (race with session creation);
                        # ignore silently — the set starts empty and this is
                        # best-effort.
                        self._send_json(200, {"ok": True, "no_session": True})
                        return
                    session.pending_background_agent_ids.add(agent_id)
                    pending_count = len(session.pending_background_agent_ids)
                self._send_json(200, {"ok": True, "pending": pending_count})
            except Exception as exc:
                self._send_error(400, str(exc))

        def _handle_rate_limit_hook(self) -> None:
            """Complete the active request with a rate-limit error when the drain thread
            detects the /rate-limit-options TUI.

            The drain thread injects Enter to dismiss the TUI, then POSTs here so the
            waiting caller gets an immediate error instead of hanging until the hard
            30-minute timeout.

            NOTE: Whether Claude stays alive after the TUI is dismissed or eventually
            exits needs empirical confirmation.  Currently finish_request_for_route is
            called with promote=True (the default), which means a queued request would
            be promoted and routed to Claude immediately.  If Claude remains blocked on
            the rate limit, that promoted request will hang until the limit resets or the
            stall watchdog kills the process.  If this becomes a problem in practice,
            switch to promote=False here so the queue is not advanced into a
            rate-limited process.
            TODO: validate on a real rate-limit event and update accordingly.

            Idempotent: silently no-ops if there is no active request (e.g. the real
            Stop hook already completed it) or if the route is unknown.
            """
            try:
                payload = self._read_json()
                route_key = payload.get("route_key")
                if not route_key:
                    raise RuntimeError("rate-limit hook payload missing route_key")
                error_msg = (
                    "Org monthly spend limit reached — Claude has paused and saved the "
                    "session.  It will resume automatically when the limit resets."
                )
                with state.lock:
                    session = state.registry._sessions.get(route_key)
                    if session is None:
                        self._send_json(200, {"ok": True, "no_session": True})
                        return
                    req = session.active_request
                    if (
                        req is None
                        or req.response is not None
                        or req.request_id in session.completed_request_ids
                    ):
                        # Already completed (Stop hook beat us, or no active request).
                        self._send_json(200, {"ok": True, "no_active_request": True})
                        return
                    state.registry.finish_request_for_route(
                        route=route_key,
                        request_id=req.request_id,
                        response=error_msg,
                    )
                    state.condition.notify_all()
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_error(400, str(exc))

        def _handle_subagent_stop_hook(self) -> None:
            """Remove a completed background-agent agentId from the session's tracking set.

            Called by the SubagentStop hook when a subagent session ends.  Discards
            the agentId from pending_background_agent_ids (a no-op if the id is
            absent — safe for stale calls from sync agents) and wakes up the
            condition variable so the Stop hook handler or _wait_for_response() can
            re-evaluate.
            """
            try:
                payload = self._read_json()
                session_id = _canonical_session_id(payload.get("session_id"))
                if session_id is None:
                    raise RuntimeError("subagent-stop payload missing session_id")
                agent_id = payload.get("agent_id")
                workdir = str(payload.get("cwd") or os.getcwd())
                with state.lock:
                    session = state.registry.get(session_id, workdir=workdir)
                    if session is None:
                        self._send_json(200, {"ok": True, "no_session": True})
                        return
                    if isinstance(agent_id, str) and agent_id:
                        # Track completed agents so the transcript-scan logic can
                        # avoid re-adding agents that already finished.
                        session.completed_agent_ids.add(agent_id)
                        session.pending_background_agent_ids.discard(agent_id)
                    pending_count = len(session.pending_background_agent_ids)
                    state.condition.notify_all()
                self._send_json(200, {"ok": True, "pending": pending_count})
            except Exception as exc:
                self._send_error(400, str(exc))

    return Handler


class _FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that skips the reverse-DNS lookup in server_bind.

    The stock HTTPServer.server_bind calls socket.getfqdn() to populate
    self.server_name.  On macOS this reverse-DNS lookup for 127.0.0.1 can
    block for 30+ seconds when the process is spawned in a new session (no
    shared DNS cache).  Since we only ever bind to loopback and never use
    server_name, we skip the lookup and hard-code "localhost".
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def serve(*, state_file: Path, host: str = "127.0.0.1", port: int = 0) -> int:
    state = ControlState(state_dir=state_file.parent)
    server = _FastBindHTTPServer((host, port), make_handler(state))
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
                    # Wake handler threads blocked on activation_event.wait() so
                    # they can observe shutdown_requested and return a 503 error.
                    # (Mirrors the POST /shutdown handler's cancel step.)
                    for _session in state.registry._sessions.values():
                        state.registry.cancel_queued_requests_for_route(route=_session.route_key)
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

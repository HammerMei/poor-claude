"""Session and pending-request registries."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


def canonical_project_dir(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def route_key(*, project_dir: str, session_id: str) -> str:
    return f"{canonical_project_dir(project_dir)}::{session_id}"


@dataclass
class PendingRequest:
    request_id: str
    prompt: str
    created_at: float
    timeout_seconds: int
    response: str | None = None
    # First Claude turn before background agents complete (e.g. "LAUNCHED").
    # Set once (first-one-wins) by either _handle_stop_hook (when it defers) or
    # _wait_for_response (when it detects a background agent in the transcript).
    # Prepended to the final response so callers see the full turn history.
    intermediate_response: str | None = None


@dataclass
class SessionRecord:
    session_id: str
    route_key: str
    auto_created: bool
    ttl_seconds: int | None
    keep_alive: bool
    workdir: str
    created_at: float
    last_request_finished_at: float | None = None
    active_request: PendingRequest | None = None
    completed_request_ids: deque[str] = field(default_factory=lambda: deque(maxlen=256))
    metadata: dict[str, str] = field(default_factory=dict)
    # Tracks background work that is still in-flight for the active request.
    #
    # Holds two kinds of IDs that are managed identically but have different
    # completion signals:
    #
    #   • Agent IDs (``Agent(run_in_background=True)``)
    #     Discovered by the Stop hook / transcript-polling loop via
    #     ``find_background_agent_ids_in_transcript``.  Removed when the
    #     matching ``SubagentStop`` hook fires.
    #
    #   • Bash task IDs (``Bash(run_in_background=True)``)
    #     Discovered by the same paths via ``find_background_task_ids_in_transcript``.
    #     Removed when ``find_completed_task_ids_in_transcript`` finds a terminal
    #     ``<task-notification>`` — there is no SubagentStop equivalent for Bash tasks.
    #
    # The Stop hook is deferred until this set is empty so we wait for all
    # background work to finish before signalling ACG.  Set semantics make
    # stale SubagentStop calls (e.g. from sync agents) safe no-ops via discard().
    #
    # ID-collision invariant: agent IDs match ``[A-Za-z0-9_-]+`` and Bash task
    # IDs match ``[a-z0-9]+`` (observed format; verified against real transcripts).
    # The task-ID regex only matches lowercase alphanumeric, so agent IDs that
    # include uppercase letters or hyphens cannot alias a task ID.  If Claude Code
    # ever changes the task-ID format, this assumption must be revisited.
    pending_background_agent_ids: set = field(default_factory=set)
    # Accumulates IDs of background work that has *completed* during the current
    # request.  Used by the transcript-scan logic to avoid re-adding items that
    # already finished before the scan ran:
    #   • For Agent tasks: populated when SubagentStop fires.
    #   • For Bash tasks: populated when find_completed_task_ids_in_transcript
    #     discovers a terminal <task-notification>.
    # A SubagentStop / completion scan that fires before the transcript scan would
    # otherwise cause an infinite defer loop.  Reset at the start of each new
    # request alongside pending_background_agent_ids.
    completed_agent_ids: set = field(default_factory=set)

    def is_idle_expired(self, now: float | None = None) -> bool:
        if self.keep_alive or self.ttl_seconds is None or self.active_request is not None:
            return False
        reference = self.last_request_finished_at or self.created_at
        current = time.time() if now is None else now
        return current - reference >= self.ttl_seconds


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def get(self, session_id: str, *, workdir: str) -> SessionRecord | None:
        return self._sessions.get(route_key(project_dir=workdir, session_id=session_id))

    def create_or_get(
        self,
        *,
        session_id: str | None,
        ttl_seconds: int | None,
        keep_alive: bool,
        workdir: str,
        now: float | None = None,
    ) -> SessionRecord:
        resolved_id = session_id or str(uuid.uuid4())
        resolved_workdir = canonical_project_dir(workdir)
        resolved_route_key = route_key(project_dir=resolved_workdir, session_id=resolved_id)
        existing = self._sessions.get(resolved_route_key)
        if existing is not None:
            return existing
        record = SessionRecord(
            session_id=resolved_id,
            route_key=resolved_route_key,
            auto_created=session_id is None,
            ttl_seconds=ttl_seconds,
            keep_alive=keep_alive,
            workdir=resolved_workdir,
            created_at=time.time() if now is None else now,
        )
        self._sessions[resolved_route_key] = record
        return record

    def start_request(
        self,
        *,
        session_id: str,
        prompt: str,
        timeout_seconds: int,
        now: float | None = None,
    ) -> PendingRequest:
        matches = [session for session in self._sessions.values() if session.session_id == session_id]
        if len(matches) != 1:
            raise RuntimeError("session lookup by session_id is ambiguous; use start_request_for_route")
        record = matches[0]
        return self.start_request_for_route(
            route=record.route_key,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            now=now,
        )

    def start_request_for_route(
        self,
        *,
        route: str,
        prompt: str,
        timeout_seconds: int,
        now: float | None = None,
    ) -> PendingRequest:
        record = self._sessions[route]
        if record.active_request is not None:
            raise RuntimeError(f"request already in progress for route {route}")
        request = PendingRequest(
            request_id=uuid.uuid4().hex,
            prompt=prompt,
            created_at=time.time() if now is None else now,
            timeout_seconds=timeout_seconds,
        )
        record.active_request = request
        record.pending_background_agent_ids = set()
        record.completed_agent_ids = set()
        return request

    def finish_request(
        self,
        *,
        session_id: str,
        request_id: str | None,
        response: str,
        now: float | None = None,
    ) -> PendingRequest:
        matches = [session for session in self._sessions.values() if session.session_id == session_id]
        if len(matches) != 1:
            raise RuntimeError("session lookup by session_id is ambiguous; use finish_request_for_route")
        record = matches[0]
        return self.finish_request_for_route(
            route=record.route_key,
            request_id=request_id,
            response=response,
            now=now,
        )

    def finish_request_for_route(
        self,
        *,
        route: str,
        request_id: str | None,
        response: str,
        now: float | None = None,
    ) -> PendingRequest:
        record = self._sessions[route]
        request = record.active_request
        if request is None:
            raise RuntimeError(f"no active request for route {route}")
        if request_id is not None and request.request_id != request_id:
            raise RuntimeError("request id mismatch")
        request.response = response
        record.active_request = None
        record.completed_request_ids.append(request.request_id)
        record.last_request_finished_at = time.time() if now is None else now
        return request

    def timeout_request_for_route(
        self,
        *,
        route: str,
        request_id: str,
        now: float | None = None,
    ) -> PendingRequest:
        record = self._sessions[route]
        request = record.active_request
        if request is None:
            raise RuntimeError(f"no active request for route {route}")
        if request.request_id != request_id:
            raise RuntimeError("request id mismatch")
        record.active_request = None
        record.completed_request_ids.append(request.request_id)
        record.last_request_finished_at = time.time() if now is None else now
        return request

    def expired_idle_sessions(self, *, now: float | None = None) -> list[SessionRecord]:
        return [session for session in self._sessions.values() if session.is_idle_expired(now)]

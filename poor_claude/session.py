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

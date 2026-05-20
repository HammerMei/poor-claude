"""Session and pending-request registries."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# Maximum number of requests that may wait in a session's queue.
# Requests beyond this limit are rejected with TooManyRequestsError.
MAX_PENDING_QUEUE_DEPTH: int = 10


class TooManyRequestsError(RuntimeError):
    """Raised when a session's pending request queue is full.

    The caller should surface this as HTTP 429 so the client can back off and retry.
    """


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
    # Set when this request is promoted from the pending queue to active status.
    # Handler threads wait on this event before calling route_prompt so that
    # the response timeout starts at the moment Claude begins processing —
    # not while the request is waiting in queue behind another request.
    activation_event: threading.Event = field(default_factory=threading.Event)
    # Set to True when the request is cancelled before it could be activated
    # (e.g. daemon shutdown, session stop, or previous-request timeout).
    # The handler thread checks this flag after activation_event fires.
    cancelled: bool = False


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
    # FIFO queue of requests waiting to become active.  A plain deque (no maxlen)
    # so we can check the depth and reject with TooManyRequestsError rather than
    # silently dropping.  The invariant is: pending_request_queue is non-empty
    # only when active_request is not None (a new request is always promoted
    # immediately if the session is idle).
    pending_request_queue: deque[PendingRequest] = field(default_factory=deque)
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
    # contain at least one uppercase letter or ``[-_]`` character cannot alias a
    # task ID.  However, an all-lowercase-alphanumeric agent ID (e.g. "abc123")
    # is indistinguishable from a task ID by format alone — a SubagentStop for
    # such an agent would incorrectly discard a same-named pending Bash task and
    # vice versa, causing silent under-counting and premature request completion.
    # In practice Claude Code always generates agent IDs with uppercase letters or
    # hyphens, so collisions have not been observed.  If Claude Code ever changes
    # its agent-ID format, the sets should be split into separate agent and task
    # ID tracking structures.
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
        # pending_request_queue is non-empty only when active_request is not None
        # (invariant enforced by enqueue_or_activate_request_for_route), so the
        # active_request check below is sufficient to cover both cases.
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

    def enqueue_or_activate_request_for_route(
        self,
        *,
        route: str,
        prompt: str,
        timeout_seconds: int,
        now: float | None = None,
    ) -> tuple[PendingRequest, bool]:
        """Create a request and either activate it immediately or enqueue it.

        Returns ``(request, is_active_immediately)``.

        - ``is_active_immediately=True``: the session was idle; the request is
          now the active request and ``request.activation_event`` is already set.
          The caller should proceed to ``route_prompt`` without waiting.

        - ``is_active_immediately=False``: another request is running; the new
          request has been appended to ``pending_request_queue``.  The caller
          must block on ``request.activation_event`` and check ``request.cancelled``
          before calling ``route_prompt``.

        Raises ``TooManyRequestsError`` if the queue depth would exceed
        ``MAX_PENDING_QUEUE_DEPTH``.

        Timeout semantics: ``timeout_seconds`` is stored on the request but the
        clock does *not* start until the request is activated.  The handler thread
        is responsible for passing ``request.timeout_seconds`` to
        ``_wait_for_response`` after activation so the deadline is measured from
        the moment Claude starts processing, not from enqueue time.
        """
        record = self._sessions[route]
        request = PendingRequest(
            request_id=uuid.uuid4().hex,
            prompt=prompt,
            created_at=time.time() if now is None else now,
            timeout_seconds=timeout_seconds,
        )
        if record.active_request is None:
            # Session is idle — activate immediately.
            record.active_request = request
            record.pending_background_agent_ids = set()
            record.completed_agent_ids = set()
            request.activation_event.set()
            return request, True
        # Another request is already active — queue this one.
        if len(record.pending_request_queue) >= MAX_PENDING_QUEUE_DEPTH:
            raise TooManyRequestsError(
                f"session queue is full ({MAX_PENDING_QUEUE_DEPTH} requests pending); "
                "retry after the current request completes"
            )
        record.pending_request_queue.append(request)
        return request, False

    def _promote_next_queued_request(self, record: SessionRecord, now: float | None) -> None:
        """Promote the oldest queued request to active status.

        Must be called under the caller's lock with ``record.active_request`` already
        cleared.  Sets the promoted request's ``activation_event`` so the handler
        thread unblocks and proceeds to ``route_prompt``.
        """
        if record.pending_request_queue:
            next_req = record.pending_request_queue.popleft()
            record.active_request = next_req
            record.pending_background_agent_ids = set()
            record.completed_agent_ids = set()
            next_req.activation_event.set()

    def finish_request_for_route(
        self,
        *,
        route: str,
        request_id: str | None,
        response: str,
        now: float | None = None,
        promote: bool = True,
    ) -> PendingRequest:
        """Mark the active request complete and optionally promote the next queued one.

        ``promote=True`` (the default) immediately activates the next queued request so
        the session can continue processing without delay.

        ``promote=False`` leaves queued requests untouched.  Use this when the session
        is about to lose its process (e.g. timeout path with a transcript fallback) so
        that callers can cancel the queue themselves rather than waking a handler thread
        that would call ``route_prompt`` against a dead route.
        """
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
        if promote:
            self._promote_next_queued_request(record, now)
        return request

    def timeout_request_for_route(
        self,
        *,
        route: str,
        request_id: str,
        now: float | None = None,
    ) -> PendingRequest:
        """Mark the active request as timed-out and cancel any queued requests.

        On timeout the Claude process is killed; queued requests cannot be served
        until the session restarts.  Rather than holding them in limbo, cancel them
        immediately so callers receive an error and can retry (which will trigger a
        process restart on the next enqueue_or_activate_request_for_route call).
        """
        record = self._sessions[route]
        request = record.active_request
        if request is None:
            raise RuntimeError(f"no active request for route {route}")
        if request.request_id != request_id:
            raise RuntimeError("request id mismatch")
        record.active_request = None
        record.completed_request_ids.append(request.request_id)
        record.last_request_finished_at = time.time() if now is None else now
        self.cancel_queued_requests_for_route(route=route)
        return request

    def finish_and_cancel_queue_for_route(
        self,
        *,
        route: str,
        request_id: str | None,
        response: str,
        now: float | None = None,
    ) -> PendingRequest:
        """Complete the active request and immediately cancel any queued requests.

        Used in the timeout+transcript path where the Claude process is about to
        be killed: the active request gets a response (from the transcript), and
        all queued requests are cancelled so their handler threads receive an error
        immediately rather than waking up and calling ``route_prompt`` against a
        dead route.

        Equivalent to ``finish_request_for_route(promote=False)`` followed by
        ``cancel_queued_requests_for_route``, but in a single call so callers
        cannot forget the cancel step.
        """
        finished = self.finish_request_for_route(
            route=route,
            request_id=request_id,
            response=response,
            now=now,
            promote=False,
        )
        self.cancel_queued_requests_for_route(route=route)
        return finished

    def cancel_queued_requests_for_route(self, *, route: str) -> list[PendingRequest]:
        """Cancel and drain all queued (not yet active) requests for a route.

        Sets ``cancelled=True`` and fires ``activation_event`` on each request so
        that handler threads blocking on ``activation_event.wait()`` wake up and
        return an error to their callers.

        Returns the list of cancelled requests (may be empty).
        """
        record = self._sessions[route]
        cancelled = list(record.pending_request_queue)
        record.pending_request_queue.clear()
        for req in cancelled:
            req.cancelled = True
            req.activation_event.set()
        return cancelled

    def expired_idle_sessions(self, *, now: float | None = None) -> list[SessionRecord]:
        return [session for session in self._sessions.values() if session.is_idle_expired(now)]

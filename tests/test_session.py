import threading

import pytest

from poor_claude.session import MAX_PENDING_QUEUE_DEPTH, SessionRegistry, TooManyRequestsError


def test_create_auto_session_generates_id_and_expires_after_ttl() -> None:
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id=None,
        ttl_seconds=10,
        keep_alive=False,
        workdir="/tmp",
        now=100,
    )
    assert len(session.session_id) == 36
    assert session.auto_created is True
    assert session.is_idle_expired(now=109) is False
    assert session.is_idle_expired(now=110) is True


def test_named_session_reuses_existing_record() -> None:
    registry = SessionRegistry()
    first = registry.create_or_get(
        session_id="demo",
        ttl_seconds=10,
        keep_alive=False,
        workdir="/tmp",
        now=100,
    )
    second = registry.create_or_get(
        session_id="demo",
        ttl_seconds=20,
        keep_alive=True,
        workdir="/tmp",
        now=200,
    )
    assert first is second
    assert second.ttl_seconds == 10


def test_same_session_id_in_different_workdirs_creates_distinct_routes() -> None:
    registry = SessionRegistry()
    first = registry.create_or_get(
        session_id="demo",
        ttl_seconds=10,
        keep_alive=False,
        workdir="/tmp",
        now=100,
    )
    second = registry.create_or_get(
        session_id="demo",
        ttl_seconds=20,
        keep_alive=True,
        workdir="/var/tmp",
        now=200,
    )
    assert first is not second
    assert first.route_key != second.route_key


def test_idle_session_activates_immediately() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    request, is_active = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="hello", timeout_seconds=300, now=101
    )
    assert is_active is True
    assert request.activation_event.is_set()
    assert session.active_request is request


def test_second_request_is_queued_when_session_busy() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, active1 = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, active2 = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    assert active1 is True
    assert active2 is False
    assert not r2.activation_event.is_set()
    assert len(session.pending_request_queue) == 1
    assert session.active_request is r1


def test_finish_promotes_next_queued_request() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    # R1 completes — R2 should be promoted automatically.
    finished = registry.finish_request_for_route(
        route=session.route_key, request_id=r1.request_id, response="ok", now=103
    )
    assert finished.response == "ok"
    assert session.active_request is r2
    assert r2.activation_event.is_set()
    assert len(session.pending_request_queue) == 0
    assert session.last_request_finished_at == 103


def test_queue_fifo_ordering() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    r3, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="third", timeout_seconds=300, now=103
    )
    # R1 done → R2 active
    registry.finish_request_for_route(route=session.route_key, request_id=r1.request_id, response="a", now=104)
    assert session.active_request is r2
    # R2 done → R3 active
    registry.finish_request_for_route(route=session.route_key, request_id=r2.request_id, response="b", now=105)
    assert session.active_request is r3


def test_queue_full_raises_too_many_requests() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    # Fill the active slot + the full queue.
    for i in range(MAX_PENDING_QUEUE_DEPTH + 1):
        registry.enqueue_or_activate_request_for_route(
            route=session.route_key, prompt=f"req{i}", timeout_seconds=300, now=float(100 + i)
        )
    with pytest.raises(TooManyRequestsError):
        registry.enqueue_or_activate_request_for_route(
            route=session.route_key, prompt="overflow", timeout_seconds=300, now=200
        )


def test_timeout_cancels_queued_requests() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    r3, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="third", timeout_seconds=300, now=103
    )
    # R1 times out — R2 and R3 in queue should be cancelled.
    registry.timeout_request_for_route(route=session.route_key, request_id=r1.request_id, now=200)
    assert session.active_request is None
    assert len(session.pending_request_queue) == 0
    assert r2.cancelled is True
    assert r2.activation_event.is_set()
    assert r3.cancelled is True
    assert r3.activation_event.is_set()


def test_cancel_queued_requests_for_route() -> None:
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    cancelled = registry.cancel_queued_requests_for_route(route=session.route_key)
    assert len(cancelled) == 1
    assert cancelled[0] is r2
    assert r2.cancelled is True
    assert r2.activation_event.is_set()
    # Active request (r1) is not touched.
    assert session.active_request is r1


def test_activation_event_set_in_separate_thread() -> None:
    """activation_event.wait() unblocks when finish_request_for_route promotes R2."""
    registry = SessionRegistry()
    registry.create_or_get(session_id="demo", ttl_seconds=10, keep_alive=False, workdir="/tmp", now=100)
    session = registry.get("demo", workdir="/tmp")
    r1, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="first", timeout_seconds=300, now=101
    )
    r2, _ = registry.enqueue_or_activate_request_for_route(
        route=session.route_key, prompt="second", timeout_seconds=300, now=102
    )
    woke = threading.Event()

    def waiter() -> None:
        r2.activation_event.wait(timeout=5)
        woke.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    registry.finish_request_for_route(route=session.route_key, request_id=r1.request_id, response="done", now=103)
    assert woke.wait(timeout=5), "activation_event.wait() did not unblock within 5 seconds"


def test_keep_alive_session_never_expires() -> None:
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=None,
        keep_alive=True,
        workdir="/tmp",
        now=100,
    )
    assert session.is_idle_expired(now=999999) is False

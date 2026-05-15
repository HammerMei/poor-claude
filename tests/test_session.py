import pytest

from poor_claude.session import SessionRegistry


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


def test_single_flight_per_session() -> None:
    registry = SessionRegistry()
    registry.create_or_get(
        session_id="demo",
        ttl_seconds=10,
        keep_alive=False,
        workdir="/tmp",
        now=100,
    )
    session = registry.get("demo", workdir="/tmp")
    request = registry.start_request_for_route(
        route=session.route_key,
        prompt="hello",
        timeout_seconds=300,
        now=101,
    )
    with pytest.raises(RuntimeError, match="already in progress"):
        registry.start_request_for_route(
            route=session.route_key,
            prompt="world",
            timeout_seconds=300,
            now=102,
        )
    finished = registry.finish_request_for_route(
        route=session.route_key,
        request_id=request.request_id,
        response="ok",
        now=103,
    )
    assert finished.response == "ok"
    assert registry.get("demo", workdir="/tmp").last_request_finished_at == 103


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

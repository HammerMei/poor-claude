import os
import threading
import time

from poor_claude import daemon as daemon_module
from poor_claude.daemon import DaemonState, discover_state, read_state, start_daemon, write_state


def test_daemon_state_roundtrip(tmp_path) -> None:
    path = tmp_path / "daemon.json"
    write_state(path, DaemonState(pid=os.getpid(), address="127.0.0.1:1234"))
    state = read_state(path)
    assert state == DaemonState(pid=os.getpid(), address="127.0.0.1:1234")
    assert discover_state(path) == state


def test_discover_state_removes_stale_pid(tmp_path) -> None:
    path = tmp_path / "daemon.json"
    write_state(path, DaemonState(pid=999999999, address="127.0.0.1:1234"))
    assert discover_state(path) is None


def test_discover_state_treats_corrupt_file_as_no_daemon(tmp_path) -> None:
    """Regression test: an unparseable state file must not crash discover_state
    (and therefore every start_daemon()/CLI caller that goes through it) — it
    should be treated the same as "no daemon running"."""
    path = tmp_path / "daemon.json"
    path.write_text("not valid json", encoding="utf-8")
    assert discover_state(path) is None


def test_discover_state_does_not_delete_corrupt_file(tmp_path) -> None:
    """Regression test: unlike the stale-pid case, discover_state() must NOT
    delete a corrupt file itself — this runs with no lock held, so deleting
    here could race a concurrent daemon's just-written valid state. It's fine
    to leave it: the next successful spawn's write_state() atomically
    overwrites it."""
    path = tmp_path / "daemon.json"
    path.write_text("not valid json", encoding="utf-8")
    discover_state(path)
    assert path.exists()


def test_discover_state_wrong_field_type_treated_as_no_daemon(tmp_path) -> None:
    path = tmp_path / "daemon.json"
    path.write_text('{"pid": null, "address": "http://127.0.0.1:1"}', encoding="utf-8")
    assert discover_state(path) is None


def test_start_daemon_serializes_concurrent_callers(tmp_path, monkeypatch) -> None:
    """Regression test: concurrent start_daemon() callers that all observe
    "no daemon running yet" must not each spawn their own daemon process —
    only one spawn should happen, and everyone else should discover that
    winner's state. This reproduces a real incident where several routes
    starting up around the same time each spawned a duplicate control_server,
    and the duplicates fought over the same session, crashing most of them."""
    path = tmp_path / "daemon.json"
    popen_calls: list[list[str]] = []
    popen_lock = threading.Lock()

    # Avoid a real HTTP round-trip to /healthz — the fake daemon below never
    # binds a socket, it just writes a state file after a short delay to
    # simulate a slow cold start.
    monkeypatch.setattr(daemon_module, "_discover_and_verify", daemon_module.discover_state)

    def fake_popen(command, **kwargs):
        with popen_lock:
            popen_calls.append(command)

        def spawn_later() -> None:
            time.sleep(0.1)
            write_state(path, DaemonState(pid=os.getpid(), address="http://127.0.0.1:9"))

        threading.Thread(target=spawn_later).start()
        return object()

    monkeypatch.setattr(daemon_module.subprocess, "Popen", fake_popen)

    results: list[DaemonState] = []
    results_lock = threading.Lock()

    def call() -> None:
        state = start_daemon(state_path=path, timeout_seconds=5.0)
        with results_lock:
            results.append(state)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert len(popen_calls) == 1, f"expected exactly one spawn, got {len(popen_calls)}"
    assert len(results) == 5
    assert all(r == results[0] for r in results)

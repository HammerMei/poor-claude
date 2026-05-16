from pathlib import Path
import threading
import time

import pytest

from poor_claude.launcher import ClaudeLaunchSpec
from poor_claude.process_manager import ProcessManager


class FakeProcess:
    _next_pid = 1000

    def __init__(self) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FailingProcess(FakeProcess):
    def terminate(self) -> None:
        raise RuntimeError("terminate failed")


class SlowStoppingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def wait(self, timeout=None):
        self.started.set()
        self.release.wait(timeout=timeout)
        self.terminated = True
        return 0


def make_spec() -> ClaudeLaunchSpec:
    return ClaudeLaunchSpec(
        session_id="demo",
        settings_path=Path("/tmp/settings.json"),
        mcp_config_path=Path("/tmp/mcp.json"),
        channel_name="poor-claude",
        workdir=Path("/tmp"),
    )


def test_process_manager_reuses_live_process() -> None:
    launched = []

    def launch(_spec):
        proc = FakeProcess()
        launched.append(proc)
        return proc

    manager = ProcessManager(launch_fn=launch)
    first = manager.ensure_running(route_key="route", spec=make_spec())
    second = manager.ensure_running(route_key="route", spec=make_spec())
    assert first is second
    assert len(launched) == 1


def test_process_manager_stops_process() -> None:
    proc = FakeProcess()
    manager = ProcessManager(launch_fn=lambda _spec: proc)
    manager.ensure_running(route_key="route", spec=make_spec())
    assert manager.stop("route") is True
    assert proc.terminated is True
    assert manager.get("route") is None


def test_process_manager_reaps_dead_process_pty() -> None:
    proc = FakeProcess()
    proc.terminated = True
    proc._poor_claude_pty_master_fd = 999999
    manager = ProcessManager(launch_fn=lambda _spec: proc)
    manager.ensure_running(route_key="route", spec=make_spec())
    assert manager.get("route") is None
    assert proc._poor_claude_pty_master_fd is None


def test_process_manager_stop_all_is_best_effort() -> None:
    failing = FailingProcess()
    failing._poor_claude_pty_master_fd = 999999
    second = FakeProcess()
    processes = [failing, second]
    manager = ProcessManager(launch_fn=lambda _spec: processes.pop(0))
    manager.ensure_running(route_key="one", spec=make_spec())
    manager.ensure_running(route_key="two", spec=make_spec())
    try:
        manager.stop_all()
    except RuntimeError as exc:
        assert "failed to stop" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected stop_all aggregate failure")
    assert second.terminated is True
    assert manager.get("two") is None
    assert manager.get("one") is not None
    assert failing._poor_claude_pty_master_fd == 999999


def test_process_manager_stop_missing_route_returns_false() -> None:
    manager = ProcessManager(launch_fn=lambda _spec: FakeProcess())
    assert manager.stop("missing") is False


def test_process_manager_rejects_ensure_running_while_stopping() -> None:
    proc = SlowStoppingProcess()
    manager = ProcessManager(launch_fn=lambda _spec: proc)
    manager.ensure_running(route_key="route", spec=make_spec())

    def stop_route() -> None:
        manager.stop("route", timeout_seconds=1)

    thread = threading.Thread(target=stop_route)
    thread.start()
    assert proc.started.wait(timeout=1)
    with pytest.raises(RuntimeError, match="stopping"):
        manager.ensure_running(route_key="route", spec=make_spec())
    proc.release.set()
    thread.join(timeout=1)
    assert manager.get("route") is None


def test_process_manager_get_returns_stopping_process() -> None:
    proc = SlowStoppingProcess()
    manager = ProcessManager(launch_fn=lambda _spec: proc)
    first = manager.ensure_running(route_key="route", spec=make_spec())

    thread = threading.Thread(target=lambda: manager.stop("route", timeout_seconds=1))
    thread.start()
    assert proc.started.wait(timeout=1)
    assert manager.get("route") is first
    proc.release.set()
    thread.join(timeout=1)

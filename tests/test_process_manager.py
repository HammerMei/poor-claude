from pathlib import Path

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

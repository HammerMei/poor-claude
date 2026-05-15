import os

from poor_claude.daemon import DaemonState, discover_state, read_state, write_state


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
    assert not path.exists()

"""Daemon discovery state helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonState:
    pid: int
    address: str


def write_state(path: Path, state: DaemonState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": state.pid, "address": state.address}), encoding="utf-8")


def read_state(path: Path) -> DaemonState | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return DaemonState(pid=int(data["pid"]), address=str(data["address"]))


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover_state(path: Path) -> DaemonState | None:
    state = read_state(path)
    if state is None:
        return None
    if not is_pid_alive(state.pid):
        path.unlink(missing_ok=True)
        return None
    return state


def default_state_path() -> Path:
    return Path(os.environ.get("POOR_CLAUDE_STATE", "~/.poor-claude/daemon.json")).expanduser()


def start_daemon(*, state_path: Path | None = None, timeout_seconds: float = 5.0) -> DaemonState:
    """Start the background control daemon and wait for its state file."""
    path = default_state_path() if state_path is None else state_path
    existing = discover_state(path)
    if existing is not None:
        return existing

    command = [
        sys.executable,
        "-m",
        "poor_claude.control_server",
        "--state-file",
        str(path),
    ]
    subprocess.Popen(  # noqa: S603 - launches this package's daemon module
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = discover_state(path)
        if state is not None:
            return state
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for poor-claude daemon to start")

"""Daemon discovery state helpers."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonState:
    pid: int
    address: str


def write_state(path: Path, state: DaemonState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"pid": state.pid, "address": state.address}), encoding="utf-8")
    tmp.replace(path)


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
    try:
        state = read_state(path)
    except (OSError, ValueError, KeyError, TypeError):
        # Unparseable state file: treat like "no daemon" rather than crashing
        # every caller (e.g. start_daemon()). Deliberately do NOT unlink here —
        # this runs with no lock held, so deleting could race a concurrent
        # daemon's just-written, valid state (the same class of bug the
        # start_daemon() flock and _owns_current_state() guard against
        # elsewhere). Leaving it is harmless: the next successful spawn's
        # write_state() atomically overwrites it anyway.
        return None
    if state is None:
        return None
    if not is_pid_alive(state.pid):
        path.unlink(missing_ok=True)
        return None
    return state


def default_state_path() -> Path:
    return Path(os.environ.get("POOR_CLAUDE_STATE", "~/.poor-claude/daemon.json")).expanduser()


def start_daemon(*, state_path: Path | None = None, timeout_seconds: float = 30.0) -> DaemonState:
    """Start the background control daemon and wait for its state file.

    Spawns the daemon subprocess if not already running, then polls until the
    daemon writes its state file AND its HTTP server responds to /healthz.
    Uses a generous default timeout because Python cold-start (first import of
    the module tree) can take several seconds on some machines.

    Concurrent callers (e.g. several routes starting up around the same time)
    serialize on a lock file so only one of them ever spawns a daemon process;
    the rest just discover the winner's state once it's up. Without this lock,
    every caller that observes "no daemon yet" spawns its own, and the
    resulting daemons race each other for the same routes.
    """
    path = default_state_path() if state_path is None else state_path
    existing = _discover_and_verify(path)
    if existing is not None:
        return existing

    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check now that we hold the lock: another process may have
            # already spawned (and this one may already be healthy) while we
            # were waiting for it.
            existing = _discover_and_verify(path)
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
                state = _discover_and_verify(path)
                if state is not None:
                    return state
                time.sleep(0.1)
            raise TimeoutError("timed out waiting for poor-claude daemon to start")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _discover_and_verify(path: Path) -> DaemonState | None:
    """Read daemon state and confirm the HTTP server is actually responding."""
    state = discover_state(path)
    if state is None:
        return None
    # Confirm the HTTP server is up — daemon.json may exist while the server
    # is still binding its port (small window, but avoids a confusing error).
    try:
        import urllib.request
        with urllib.request.urlopen(f"{state.address}/healthz", timeout=1.0) as resp:  # noqa: S310
            resp.read()
        return state
    except OSError:
        return None

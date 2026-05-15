"""Persistent Claude process lifecycle management."""

from __future__ import annotations

import subprocess
import time
import os
import signal
import threading
from dataclasses import dataclass
from typing import Callable

from poor_claude.launcher import ClaudeLaunchSpec, launch_claude


LaunchFn = Callable[[ClaudeLaunchSpec], subprocess.Popen]


@dataclass
class ManagedProcess:
    route_key: str
    spec: ClaudeLaunchSpec
    process: subprocess.Popen
    started_at: float

    def is_alive(self) -> bool:
        return self.process.poll() is None


class ProcessManager:
    def __init__(self, *, launch_fn: LaunchFn = launch_claude) -> None:
        self._launch_fn = launch_fn
        self._processes: dict[str, ManagedProcess] = {}

    def get(self, route_key: str) -> ManagedProcess | None:
        managed = self._processes.get(route_key)
        if managed is not None and not managed.is_alive():
            self._processes.pop(route_key, None)
            return None
        return managed

    def ensure_running(self, *, route_key: str, spec: ClaudeLaunchSpec) -> ManagedProcess:
        existing = self.get(route_key)
        if existing is not None:
            return existing
        process = self._launch_fn(spec)
        if spec.auto_accept_workspace_trust:
            _send_initial_enter_if_pty(process)
        managed = ManagedProcess(route_key=route_key, spec=spec, process=process, started_at=time.time())
        self._processes[route_key] = managed
        return managed

    def stop(self, route_key: str, *, timeout_seconds: float = 3.0) -> bool:
        managed = self._processes.pop(route_key, None)
        if managed is None:
            return False
        _terminate_process(managed.process, timeout_seconds=timeout_seconds)
        return True

    def stop_all(self, *, timeout_seconds: float = 3.0) -> None:
        for route_key in list(self._processes.keys()):
            self.stop(route_key, timeout_seconds=timeout_seconds)


def _terminate_process(process: subprocess.Popen, *, timeout_seconds: float) -> None:
    if process.poll() is not None:
        _close_pty(process)
        return
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.terminate()
        except PermissionError:
            process.terminate()
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=timeout_seconds)
    finally:
        _close_pty(process)


def _close_pty(process: subprocess.Popen) -> None:
    master_fd = getattr(process, "_poor_claude_pty_master_fd", None)
    if isinstance(master_fd, int):
        try:
            os.close(master_fd)
        except OSError:
            pass


def _send_initial_enter_if_pty(process: subprocess.Popen) -> None:
    master_fd = getattr(process, "_poor_claude_pty_master_fd", None)
    if not isinstance(master_fd, int):
        return

    def send_enter() -> None:
        for delay in (1.0, 2.0, 3.0):
            time.sleep(delay)
            if process.poll() is not None:
                return
            try:
                os.write(master_fd, b"\r")
            except OSError:
                return

    threading.Thread(target=send_enter, daemon=True).start()

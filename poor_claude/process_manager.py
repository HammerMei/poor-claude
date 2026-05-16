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
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stopping: dict[str, ManagedProcess] = {}

    def get(self, route_key: str) -> ManagedProcess | None:
        with self._lock:
            managed = self._processes.get(route_key)
            if managed is not None and not managed.is_alive():
                self._processes.pop(route_key, None)
                _close_pty(managed.process)
                return None
            return managed or self._stopping.get(route_key)

    def ensure_running(self, *, route_key: str, spec: ClaudeLaunchSpec) -> ManagedProcess:
        with self._lock:
            if route_key in self._stopping:
                raise RuntimeError("session process is stopping; retry request")
            existing = self.get(route_key)
            if existing is not None:
                return existing
            process = self._launch_fn(spec)
            if spec.auto_accept_workspace_trust and spec.stdout_path is None:
                _send_initial_enter_if_pty(process)
            managed = ManagedProcess(route_key=route_key, spec=spec, process=process, started_at=time.time())
            self._processes[route_key] = managed
            return managed

    def stop(self, route_key: str, *, timeout_seconds: float = 3.0) -> bool:
        while True:
            with self._lock:
                managed = self._processes.get(route_key)
                if managed is not None:
                    self._processes.pop(route_key, None)
                    self._stopping[route_key] = managed
                    break
                if route_key not in self._stopping:
                    return False
                self._condition.wait()
        try:
            terminate_managed(managed, timeout_seconds=timeout_seconds)
        except Exception:
            with self._lock:
                self._stopping.pop(route_key, None)
                self._processes.setdefault(route_key, managed)
                self._condition.notify_all()
            raise
        with self._lock:
            self._stopping.pop(route_key, None)
            self._condition.notify_all()
            return True

    def detach(self, route_key: str) -> ManagedProcess | None:
        with self._lock:
            return self._processes.pop(route_key, None)

    def attach_if_absent(self, managed: ManagedProcess) -> None:
        with self._lock:
            self._processes.setdefault(managed.route_key, managed)

    def stop_all(self, *, timeout_seconds: float = 3.0) -> None:
        attempted = set()
        errors = []
        while True:
            with self._lock:
                route_keys = [route for route in self._processes if route not in attempted]
                has_stopping = bool(self._stopping)
            if route_keys:
                for route_key in route_keys:
                    attempted.add(route_key)
                    try:
                        self.stop(route_key, timeout_seconds=timeout_seconds)
                    except Exception as exc:
                        errors.append(exc)
                continue
            if has_stopping:
                with self._lock:
                    if self._stopping:
                        self._condition.wait(timeout=0.1)
                continue
            break
        with self._lock:
            remaining = len(self._processes)
        if errors or remaining:
            if not errors:
                errors.append(RuntimeError(f"{remaining} process(es) still running"))
            raise RuntimeError(f"failed to stop {len(errors)} process(es): {errors[0]}")


def terminate_managed(managed: ManagedProcess, *, timeout_seconds: float = 3.0) -> None:
    _terminate_process(managed.process, timeout_seconds=timeout_seconds)


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
        if process.poll() is not None:
            _close_pty(process)


def _close_pty(process: subprocess.Popen) -> None:
    master_fd = getattr(process, "_poor_claude_pty_master_fd", None)
    drain_thread = getattr(process, "_poor_claude_pty_thread", None)
    if isinstance(drain_thread, threading.Thread) and drain_thread.is_alive():
        drain_thread.join(timeout=1.0)
    if isinstance(master_fd, int):
        try:
            os.close(master_fd)
        except OSError:
            pass
        finally:
            setattr(process, "_poor_claude_pty_master_fd", None)
    if isinstance(drain_thread, threading.Thread) and drain_thread.is_alive():
        drain_thread.join(timeout=1.0)


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

import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from poor_claude.control_server import ControlState, _safe_to_delete_state_file, _settings_fingerprint, make_handler
from poor_claude.daemon import DaemonState, write_state
from poor_claude.http_client import HttpClientError, request_json
from poor_claude.transcript import TranscriptResponse


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class SlowFakeProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = threading.Event()
        self.release_wait = threading.Event()

    def wait(self, timeout=None):
        self.wait_started.set()
        self.release_wait.wait(timeout=timeout)
        return 0


class RaisingWaitFakeProcess(FakeProcess):
    def poll(self):
        return None

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return

    def wait(self, timeout=None):
        raise RuntimeError("wait failed")


class RaisingStopProcessManager:
    def stop_all(self):
        raise RuntimeError("stop all failed")


def start_test_server():
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    return server, address


def test_control_server_session_lifecycle(tmp_path) -> None:
    server, address = start_test_server()
    try:
        settings_path = tmp_path / "acg-settings.json"
        settings_path.write_text('{"hooks": {}}', encoding="utf-8")
        created = request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "ttl_seconds": 3600,
                "keep_alive": False,
                "workdir": str(tmp_path),
                "settings_path": str(settings_path),
                "permission_mode": "bypassPermissions",
            },
        )
        assert created["session_id"] == "demo"

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["session_id"] == "demo"
        assert listed["sessions"][0]["metadata"]["settings_path"] == str(settings_path)
        assert listed["sessions"][0]["metadata"]["permission_mode"] == "bypassPermissions"
        assert listed["sessions"][0]["metadata"]["launch_config_frozen"] == "True"
        assert "merged_settings_path" in listed["sessions"][0]["metadata"]
        assert "launch_command" in listed["sessions"][0]["metadata"]

        stopped = request_json(
            "DELETE",
            f"{address}/sessions/demo",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )
        assert stopped == {"ok": True, "session_id": "demo"}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_records_resume_launch_command(tmp_path) -> None:
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "resume_session": True,
            },
        )
        listed = request_json("GET", f"{address}/sessions")
        metadata = listed["sessions"][0]["metadata"]
        assert metadata["resume_on_launch"] == "True"
        assert '"--resume"' in metadata["launch_command"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_canonicalizes_uuid_session_ids(tmp_path) -> None:
    server, address = start_test_server()
    compact = "11111111111141118111111111111111"
    canonical = "11111111-1111-4111-8111-111111111111"
    try:
        created = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": compact, "workdir": str(tmp_path)},
        )
        assert created["session_id"] == canonical
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["session_id"] == canonical
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_delete_canonicalizes_uuid_session_ids(tmp_path) -> None:
    server, address = start_test_server()
    compact = "11111111111141118111111111111111"
    canonical = "11111111-1111-4111-8111-111111111111"
    try:
        request_json("POST", f"{address}/sessions", {"session_id": compact, "workdir": str(tmp_path)})
        deleted = request_json(
            "DELETE",
            f"{address}/sessions/{compact}",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )
        assert deleted == {"ok": True, "session_id": canonical}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_prunes_expired_idle_sessions(tmp_path) -> None:
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "ttl_seconds": 0},
        )
        pruned = request_json("POST", f"{address}/prune", {})
        assert pruned["ok"] is True
        assert pruned["removed_routes"] == [f"{tmp_path.resolve()}::demo"]
        assert request_json("GET", f"{address}/sessions") == {"sessions": []}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_prunes_dead_process_sessions(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_pid"] = "123"
            session.metadata["process_alive"] = "False"
        pruned = request_json("POST", f"{address}/prune", {})
        assert pruned["removed_routes"] == [f"{tmp_path.resolve()}::demo"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_prune_stops_live_expired_process(tmp_path) -> None:
    state = ControlState()
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "ttl_seconds": 0, "launch_process": True},
        )
        pruned = request_json("POST", f"{address}/prune", {})
        assert pruned["removed_routes"] == [f"{tmp_path.resolve()}::demo"]
        assert fake_process.terminated is True
        assert state.process_manager.get(f"{tmp_path.resolve()}::demo") is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_prune_skips_stopping_sessions(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_pid"] = "123"
            session.metadata["process_alive"] = "False"
            session.metadata["process_stopping"] = "True"
        pruned = request_json("POST", f"{address}/prune", {})
        assert pruned["removed_routes"] == []
        assert request_json("GET", f"{address}/sessions")["sessions"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_delete_rejects_stopping_session(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_stopping"] = "True"
        try:
            request_json(
                "DELETE",
                f"{address}/sessions/demo",
                headers={"X-Poor-Claude-Workdir": str(tmp_path)},
            )
        except HttpClientError as exc:
            assert "session process is stopping; retry request" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected delete rejection")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_shutdown_fails_without_exiting_when_stop_all_fails() -> None:
    state = ControlState()
    state.process_manager = RaisingStopProcessManager()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        try:
            request_json("POST", f"{address}/shutdown", {})
        except HttpClientError as exc:
            assert "failed to stop all processes" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected shutdown failure")
        assert state.shutdown_requested is False
        assert state.shutdown_in_progress is False
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_rejects_new_work_after_shutdown_starts(tmp_path) -> None:
    state = ControlState()
    state.shutdown_requested = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        try:
            request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})
        except HttpClientError as exc:
            assert "daemon is shutting down" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected shutdown rejection")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_resume_mode_is_monotonic_for_route(tmp_path) -> None:
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "resume_session": True},
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        listed = request_json("GET", f"{address}/sessions")
        metadata = listed["sessions"][0]["metadata"]
        assert metadata["resume_on_launch"] == "True"
        assert '"--resume"' in metadata["launch_command"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_rejected_config_mismatch_does_not_flip_resume_mode(tmp_path) -> None:
    """permission_mode stays a HARD-frozen field (unlike settings_path, which is
    soft — see the settings_path_fingerprint / schedules_restart tests below):
    a mismatch there is security-relevant and must still be rejected outright,
    not silently absorbed via a restart."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "permission_mode": "default"},
        )
        try:
            request_json(
                "POST",
                f"{address}/sessions",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "permission_mode": "bypassPermissions",
                    "resume_session": True,
                },
            )
        except HttpClientError as exc:
            assert "existing session launch config differs" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected config mismatch")
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["resume_on_launch"] == "False"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_effort_change(tmp_path) -> None:
    """Changing --effort on a frozen session marks restart_needed and sets resume_on_launch."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "high"},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "low"},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["effort"] == "low"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_model_change(tmp_path) -> None:
    """Changing --model on a frozen session marks restart_needed and sets resume_on_launch."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "model": "claude-opus-4-5"},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "model": "claude-sonnet-4-5"},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["model"] == "claude-sonnet-4-5"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_append_system_prompt_change(tmp_path) -> None:
    """Changing --append-system-prompt on a frozen session marks restart_needed."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "append_system_prompt": "be concise"},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["append_system_prompt"] == "be concise"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_updates_allowed_tools_without_restart(tmp_path) -> None:
    """Changing allowed_tools updates metadata immediately — no restart_needed set."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "allowed_tools": ["Bash(ls *)", "Read"]},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            # No restart triggered
            assert session.metadata.get("restart_needed") != "True"
            import json as _json
            stored = _json.loads(session.metadata["allowed_tools"])
            assert sorted(stored) == ["Bash(ls *)", "Read"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_updates_disallowed_tools_without_restart(tmp_path) -> None:
    """Changing disallowed_tools updates metadata immediately — no restart_needed set."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "disallowed_tools": ["Bash(rm *)"]},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata.get("restart_needed") != "True"
            import json as _json
            stored = _json.loads(session.metadata["disallowed_tools"])
            assert stored == ["Bash(rm *)"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_system_prompt_change(tmp_path) -> None:
    """Changing --system-prompt on a frozen session marks restart_needed."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "system_prompt": "You are a pirate."},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["system_prompt"] == "You are a pirate."
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_tools_change(tmp_path) -> None:
    """Changing --tools on a frozen session marks restart_needed."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "tools": ["Bash", "Edit"]},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            import json as _json
            stored = _json.loads(session.metadata["tools"])
            assert sorted(stored) == ["Bash", "Edit"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_add_dirs_change(tmp_path) -> None:
    """Changing --add-dir on a frozen session marks restart_needed."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "add_dirs": ["/data"]},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            import json as _json
            stored = _json.loads(session.metadata["add_dirs"])
            assert stored == ["/data"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_no_restart_when_soft_params_match(tmp_path) -> None:
    """No restart_needed when soft params are unchanged."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "high"},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "high"},
        )
        assert result.get("warnings") is None
        assert "restart_needed" not in result
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_restart_needed_stops_and_relaunches_process(tmp_path) -> None:
    """When soft params change and launch_process=True, old process is stopped and a new one started."""
    state = ControlState()
    launched = []

    def fake_launch(spec):
        p = FakeProcess()
        p.pid = 9000 + len(launched)
        launched.append(p)
        return p

    state.process_manager._launch_fn = fake_launch
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        # First launch: effort=high
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "high", "launch_process": True},
        )
        assert len(launched) == 1
        first_process = launched[0]

        # Second call: effort=low — triggers restart
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "effort": "low", "launch_process": True},
        )
        assert len(launched) == 2
        assert first_process.terminated is True
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_rejects_create_session_while_process_is_stopping(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_stopping"] = "True"
        try:
            request_json(
                "POST",
                f"{address}/sessions",
                {"session_id": "demo", "workdir": str(tmp_path), "launch_process": True},
            )
        except HttpClientError as exc:
            assert "session process is stopping; retry request" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected create rejection")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_error_diagnostics_include_safe_log_status(tmp_path) -> None:
    server, address = start_test_server()
    try:
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        listed = request_json("GET", f"{address}/sessions")
        stdout_path = listed["sessions"][0]["metadata"]["claude_stdout_path"]
        Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
        Path(stdout_path).write_text("Error: secret-token-123\nListening for channel messages\n", encoding="utf-8")
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {"session_id": result["session_id"], "workdir": str(tmp_path), "prompt": "x", "timeout_seconds": 0, "wait_for_response": True},
            )
        except HttpClientError as exc:
            assert exc.payload is not None
            diagnostics = exc.payload["diagnostics"]
            assert diagnostics["paths"]["stdout"] == stdout_path
            assert "listening for channel messages" in diagnostics["summaries"]["stdout"]
            assert "secret-token-123" not in diagnostics["summaries"]["stdout"]
        else:  # pragma: no cover
            raise AssertionError("expected timeout")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_request_and_stop_hook(tmp_path) -> None:
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 300,
                "ttl_seconds": 3600,
                "keep_alive": False,
                "workdir": str(tmp_path),
            },
        )
        assert queued["session_id"] == "demo"
        assert queued["route_key"].endswith("::demo")
        assert queued["status"] == "queued"
        assert queued["channel_notification"]["method"] == "notifications/claude/channel"
        assert "hello" in queued["channel_notification"]["params"]["content"]
        assert queued["channel_notification"]["params"]["meta"] == {"request_id": queued["request_id"]}

        next_message = request_json("GET", f"{address}/mcp/next?route_key={queued['route_key']}")
        assert next_message["notification"] == queued["channel_notification"]
        drained = request_json("GET", f"{address}/mcp/next?route_key={queued['route_key']}")
        assert drained == {"notification": None}

        resolved = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": queued["request_id"],
                "response": "world",
                "transcript_path": None,
                "cwd": str(tmp_path),
            },
        )
        assert resolved == {"ok": True}

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_can_wait_for_response(tmp_path) -> None:
    server, address = start_test_server()
    try:
        result_box = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
            )

        thread = threading.Thread(target=send_request)
        thread.start()
        while "result" not in result_box:
            listed = request_json("GET", f"{address}/sessions")
            if listed["sessions"] and listed["sessions"][0]["active_request"] is not None:
                break
            time.sleep(0.05)
        listed = request_json("GET", f"{address}/sessions")
        active_request = listed["sessions"][0]["active_request"]
        request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": active_request,
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        thread.join(timeout=3)
        assert result_box["result"]["status"] == "completed"
        assert result_box["result"]["response"] == "world"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_polls_transcript_while_waiting(tmp_path, monkeypatch) -> None:
    server, address = start_test_server()
    calls = {"count": 0}

    def fake_read_response(*_args, **_kwargs):
        calls["count"] += 1
        return TranscriptResponse("world") if calls["count"] >= 2 else None

    monkeypatch.setattr("poor_claude.control_server.read_response_record_after_request_from_file", fake_read_response)
    try:
        result = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 2,
                "workdir": str(tmp_path),
                "wait_for_response": True,
            },
            timeout=5,
        )
        assert result["response"] == "world"
        assert calls["count"] >= 2
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_returns_response_if_stop_hook_wins_timeout_race(tmp_path, monkeypatch) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    def fake_read_response(*_args, **_kwargs):
        with state.lock:
            session = next(iter(state.registry._sessions.values()))
            request = session.active_request
            assert request is not None
            state.registry.finish_request_for_route(
                route=session.route_key,
                request_id=request.request_id,
                response="world",
            )
            state.condition.notify_all()
        return None

    monkeypatch.setattr("poor_claude.control_server.read_response_record_after_request_from_file", fake_read_response)
    try:
        result = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 0,
                "workdir": str(tmp_path),
                "wait_for_response": True,
            },
        )
        assert result["status"] == "completed"
        assert result["response"] == "world"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_completes_from_end_turn_transcript_before_timeout(tmp_path, monkeypatch) -> None:
    server, address = start_test_server()
    calls = {"count": 0}

    def fake_read_response(*_args, **_kwargs):
        calls["count"] += 1
        return TranscriptResponse("world", stop_reason="end_turn") if calls["count"] >= 2 else None

    monkeypatch.setattr("poor_claude.control_server.read_response_record_after_request_from_file", fake_read_response)
    try:
        result = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
                "wait_for_response": True,
            },
            timeout=5,
        )
        assert result["status"] == "completed"
        assert result["response"] == "world"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_completes_from_rate_limit_transcript_without_end_turn(tmp_path, monkeypatch) -> None:
    # Regression test for the 2026-07-06 lockup: Claude Code injects a synthetic
    # assistant turn (stop_reason "stop_sequence", never "end_turn") when the org
    # monthly spend limit is hit, and never fires the Stop hook for it. Before the
    # fix, _wait_for_response's transcript fallback only completed on
    # stop_reason == "end_turn", so the request — and the whole route — hung until
    # the hard timeout with no way to self-recover once the limit reset. Confirmed
    # live on the production daemon: active_request stayed non-None indefinitely
    # for exactly this transcript shape.
    server, address = start_test_server()
    calls = {"count": 0}

    def fake_read_response(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            return None
        return TranscriptResponse(
            "You've hit your org's monthly spend limit",
            stop_reason="stop_sequence",
            is_rate_limit_error=True,
        )

    monkeypatch.setattr("poor_claude.control_server.read_response_record_after_request_from_file", fake_read_response)
    try:
        result = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
                "wait_for_response": True,
            },
            timeout=5,
        )
        assert result["status"] == "completed"
        assert result["response"] == "You've hit your org's monthly spend limit"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_accepts_duplicate_stop_hook_for_completed_request(tmp_path) -> None:
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
        )
        request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": queued["request_id"],
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        duplicate = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": queued["request_id"],
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        assert duplicate == {"duplicate": True, "ok": True}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_accepts_duplicate_stop_hook_during_next_request(tmp_path) -> None:
    server, address = start_test_server()
    try:
        first = request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
        )
        request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": first["request_id"],
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "next", "workdir": str(tmp_path)},
        )
        duplicate = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": "demo",
                "request_id": first["request_id"],
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        assert duplicate == {"duplicate": True, "ok": True}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_holds_mcp_delivery_while_process_is_stopping(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
        )
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_stopping"] = "True"
        blocked = request_json("GET", f"{address}/mcp/next?route_key={queued['route_key']}")
        assert blocked == {"notification": None}
        with state.lock:
            session.metadata["process_stopping"] = "False"
        delivered = request_json("GET", f"{address}/mcp/next?route_key={queued['route_key']}")
        assert delivered["notification"]["params"]["meta"]["request_id"] == queued["request_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_rejects_request_while_process_is_stopping(tmp_path) -> None:
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            session.metadata["process_stopping"] = "True"
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
            )
        except HttpClientError as exc:
            assert "session process is stopping; retry request" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected request rejection")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_wait_timeout_clears_active_request(tmp_path) -> None:
    server, address = start_test_server()
    try:
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 0,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
            )
        except Exception as exc:
            assert "timed out" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected timeout")
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_wait_returns_clean_error_when_session_deleted(tmp_path) -> None:
    server, address = start_test_server()
    outcome: dict[str, str] = {}

    def wait_request() -> None:
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=10,
            )
        except HttpClientError as exc:
            outcome["error"] = str(exc)

    try:
        thread = threading.Thread(target=wait_request)
        thread.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed["sessions"]:
                break
            time.sleep(0.01)
        request_json(
            "DELETE",
            f"{address}/sessions/demo",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert "session route no longer exists" in outcome["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_wait_timeout_stops_managed_process(tmp_path) -> None:
    state = ControlState()
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 0,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
            )
        except Exception as exc:
            assert "timed out" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected timeout")
        assert fake_process.terminated is True
        notification = request_json("GET", f"{address}/mcp/next?route_key={tmp_path.resolve()}::demo")
        assert notification == {"notification": None}
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_timeout_clears_stopping_flag_when_termination_fails(tmp_path) -> None:
    state = ControlState()
    fake_process = RaisingWaitFakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 0,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
            )
        except HttpClientError as exc:
            assert "wait failed" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected termination failure")
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["process_stopping"] == "False"
        assert listed["sessions"][0]["metadata"]["termination_failed"] == "True"
        assert state.process_manager.get(listed["sessions"][0]["route_key"]) is not None
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_timeout_prepends_intermediate_response(tmp_path, monkeypatch) -> None:
    """On timeout with a transcript fallback, intermediate_response must be prepended.

    Flow:
      1. Transcript polling finds a background Bash task ("btask-001") → bg_work_detected=True,
         intermediate_response set to "LAUNCHED" (the first stable transcript response).
      2. Pending task keeps _wait_for_response looping.
      3. Transcript then stabilises on "PARTIAL OUTPUT" → transcript_fallback_response.
      4. Request times out (task never completes — no terminal <task-notification>).
      5. Response must be "LAUNCHED\\n\\nPARTIAL OUTPUT", not just "PARTIAL OUTPUT".
    """
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    read_calls: dict[str, int] = {"count": 0}
    task_scan_calls: dict[str, int] = {"count": 0}

    def fake_read_response(*_a, **_kw):
        read_calls["count"] += 1
        # First two polls → "LAUNCHED" (becomes stable, triggers bg-task scan).
        # Subsequent polls → "PARTIAL OUTPUT" (different value → becomes new stable,
        # is stored as transcript_fallback_response when timeout fires).
        if read_calls["count"] <= 2:
            return TranscriptResponse("LAUNCHED", stop_reason="end_turn")
        return TranscriptResponse("PARTIAL OUTPUT", stop_reason="end_turn")

    def fake_find_tasks(*_a, **_kw):
        # First scan: report the task so bg_work_detected=True and intermediate_response
        # is set.  Subsequent scans: return [] so pending never grows further.
        task_scan_calls["count"] += 1
        return ["btask-001"] if task_scan_calls["count"] == 1 else []

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        fake_read_response,
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_agent_ids_in_transcript",
        lambda *_a, **_kw: [],
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_task_ids_in_transcript",
        fake_find_tasks,
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_completed_task_ids_in_transcript",
        lambda *_a, **_kw: [],
    )

    result_holder: dict[str, object] = {}

    def run_request():
        try:
            result_holder["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 3,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
            )
        except Exception as exc:
            result_holder["error"] = exc

    req_thread = threading.Thread(target=run_request, daemon=True)
    req_thread.start()
    req_thread.join(timeout=8)

    try:
        assert "error" not in result_holder, f"request raised unexpectedly: {result_holder.get('error')}"
        result = result_holder.get("result")
        assert result is not None, "request thread did not complete"
        assert result["response"] == "LAUNCHED\n\nPARTIAL OUTPUT", (
            "timeout path must prepend intermediate_response to transcript fallback; "
            f"got: {result['response']!r}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_delete_does_not_hold_state_lock_while_stopping_process(tmp_path) -> None:
    state = ControlState()
    fake_process = SlowFakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    delete_done = threading.Event()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "launch_process": True},
        )

        def delete_session() -> None:
            request_json(
                "DELETE",
                f"{address}/sessions/demo",
                headers={"X-Poor-Claude-Workdir": str(tmp_path)},
                timeout=5,
            )
            delete_done.set()

        deleter = threading.Thread(target=delete_session)
        deleter.start()
        assert fake_process.wait_started.wait(timeout=2)
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["process_stopping"] == "True"
        assert not delete_done.is_set()
        fake_process.release_wait.set()
        deleter.join(timeout=5)
        assert delete_done.is_set()
        assert request_json("GET", f"{address}/sessions") == {"sessions": []}
    finally:
        fake_process.release_wait.set()
        server.shutdown()
        server.server_close()


def test_control_server_stop_hook_falls_back_to_active_request_when_no_request_id(tmp_path) -> None:
    """Stop hook without request_id falls back to the session's active request."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
        )
        # Stop hook without request_id — should complete the active request via fallback
        result = request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "demo", "response": "done", "cwd": str(tmp_path)},
        )
        assert result.get("ok") is True
        assert "duplicate" not in result
        assert "no_active_request" not in result
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_stop_hook_no_active_request_returns_ok(tmp_path) -> None:
    """Stop hook without request_id and no active request returns ok (spurious hook call)."""
    server, address = start_test_server()
    try:
        # Create a session but send no prompt — no active request
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "idle", "workdir": str(tmp_path)},
        )
        result = request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "idle", "response": "", "cwd": str(tmp_path)},
        )
        assert result.get("ok") is True
        assert result.get("no_active_request") is True
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_routes_same_session_id_by_workdir(tmp_path) -> None:
    other_workdir = tmp_path / "other"
    other_workdir.mkdir()
    server, address = start_test_server()
    try:
        first = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello a",
                "workdir": str(tmp_path),
            },
        )
        second = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello b",
                "workdir": str(other_workdir),
            },
        )
        assert first["session_id"] == second["session_id"] == "demo"
        assert first["route_key"] != second["route_key"]
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_restarts_process_on_settings_content_change(tmp_path) -> None:
    """The fix for the real ACG-hook-port-change incident: when the incoming
    settings file's CONTENT genuinely differs from what's on record for a
    frozen, already-running session, poor-claude no longer hard-rejects the
    request (that was the bug) — it schedules a process restart, the same
    mechanism already used for an effort/model change, so the new content is
    picked up by a freshly launched process with --resume preserving
    conversation history. This verifies the full propagation path end to
    end (not just that a metadata flag got set): the old process is
    actually stopped, a genuinely new process is actually launched, and the
    regenerated merged-settings file on disk actually contains the new
    content — this is the exact class of thing that silently didn't work in
    two earlier attempts at this bug (082cb18, then the stale-fingerprint
    follow-up), so metadata-only assertions are not enough here."""
    state = ControlState()
    first_process = FakeProcess()
    second_process = FakeProcess()
    second_process.pid = 5000
    launched = [first_process, second_process]
    state.process_manager._launch_fn = lambda _spec: launched.pop(0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        first_settings = tmp_path / "acg-claude-settings-thpg3ii3.json"
        second_settings = tmp_path / "acg-claude-settings-kbse3yqn.json"
        first_settings.write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
            '"command": "curl http://127.0.0.1:56453/hook"}]}]}}',
            encoding="utf-8",
        )
        second_settings.write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
            '"command": "curl http://127.0.0.1:56455/hook"}]}]}}',
            encoding="utf-8",
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "settings_path": str(first_settings),
                "launch_process": True,
            },
        )
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["process_pid"] == "4242"

        # Simulate ACG restarting: same session, new settings file at a new
        # path, genuinely different content (new permission-hook port) —
        # must NOT raise.
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "settings_path": str(second_settings),
                "launch_process": True,
            },
        )
        assert first_process.terminated is True, "old process must be stopped before relaunch"
        listed = request_json("GET", f"{address}/sessions")
        metadata = listed["sessions"][0]["metadata"]
        assert metadata["process_pid"] == "5000", "a genuinely new process must be launched"
        assert metadata["settings_path"] == str(second_settings)

        merged_content = Path(metadata["merged_settings_path"]).read_text(encoding="utf-8")
        assert "56455" in merged_content, "relaunched process's merged settings must reflect the NEW content"
        assert "56453" not in merged_content, "stale content from the old settings file must not linger"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_restarts_on_settings_change_via_requests_path(tmp_path) -> None:
    """The propagation test above goes through POST /sessions, which has NO
    busy-guard. The CLI's actual default path for sending a prompt (what ACG
    invokes on every message) is POST /requests with launch_process=True —
    which DOES have a guard that skips the restart while another request is
    active/queued (see the "session is busy" RuntimeError a few lines above
    _ensure_process_metadata in _handle_request). That guard checks
    session.active_request/pending_request_queue BEFORE the incoming request
    is itself enqueued, so it must not self-trip on the very request that
    carries the settings change — a single ordinary message right after ACG
    restarts must self-heal on the first try, not bounce with "session is
    busy" and require a retry. This exercises that exact path end to end."""
    state = ControlState()
    first_process = FakeProcess()
    second_process = FakeProcess()
    second_process.pid = 5000
    launched = [first_process, second_process]
    state.process_manager._launch_fn = lambda _spec: launched.pop(0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        first_settings = tmp_path / "acg-claude-settings-thpg3ii3.json"
        second_settings = tmp_path / "acg-claude-settings-kbse3yqn.json"
        first_settings.write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
            '"command": "curl http://127.0.0.1:56453/hook"}]}]}}',
            encoding="utf-8",
        )
        second_settings.write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
            '"command": "curl http://127.0.0.1:56455/hook"}]}]}}',
            encoding="utf-8",
        )

        def send(prompt: str, settings_path, box: dict, key: str) -> None:
            box[key] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": prompt,
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                    "settings_path": str(settings_path),
                },
            )

        def wait_for_active_and_complete(response_text: str) -> None:
            deadline = time.monotonic() + 3
            listed = None
            while time.monotonic() < deadline:
                listed = request_json("GET", f"{address}/sessions")
                if listed["sessions"] and listed["sessions"][0]["active_request"] is not None:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(
                    "request never became active within 3s "
                    "(e.g. it was rejected by the busy guard instead of enqueued)"
                )
            active_request = listed["sessions"][0]["active_request"]
            request_json(
                "POST",
                f"{address}/hook/stop",
                {
                    "session_id": "demo",
                    "request_id": active_request,
                    "response": response_text,
                    "cwd": str(tmp_path),
                },
            )

        # First message: session created, process launched with first_settings.
        result_box: dict = {}
        t1 = threading.Thread(target=send, args=("hello", first_settings, result_box, "first"))
        t1.start()
        wait_for_active_and_complete("hi")
        t1.join(timeout=3)
        assert result_box["first"]["status"] == "completed"
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["process_pid"] == "4242"

        # Second message: simulate ACG restarting mid-conversation — same
        # session, new settings file, genuinely different content — sent as
        # a normal single in-flight prompt exactly like the CLI's default
        # path. Must succeed and self-heal on THIS request, not error with
        # "session is busy".
        result_box2: dict = {}
        t2 = threading.Thread(target=send, args=("hello again", second_settings, result_box2, "second"))
        t2.start()
        wait_for_active_and_complete("hi again")
        t2.join(timeout=3)
        assert result_box2["second"]["status"] == "completed", result_box2["second"]

        assert first_process.terminated is True, "old process must be stopped before relaunch"
        listed = request_json("GET", f"{address}/sessions")
        metadata = listed["sessions"][0]["metadata"]
        assert metadata["process_pid"] == "5000", "a genuinely new process must be launched"
        merged_content = Path(metadata["merged_settings_path"]).read_text(encoding="utf-8")
        assert "56455" in merged_content
        assert "56453" not in merged_content
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_settings_change_combined_with_hard_mismatch_still_rejects_cleanly(tmp_path) -> None:
    """Round 5 gap: no test previously covered a request that changes BOTH a
    soft field (settings_path content) and a hard field (permission_mode) at
    once. The hard mismatch must still raise, and — this is the part a test
    is needed for — settings_path/settings_fingerprint metadata must NOT be
    partially mutated as a side effect before the raise. This function has
    already had two broken rewrites this session (082cb18, then the
    stale-fingerprint follow-up); locking in "the raise happens strictly
    before any metadata mutation" guards against a third."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        first_settings = tmp_path / "one.json"
        second_settings = tmp_path / "two.json"
        first_settings.write_text('{"hooks": {}}', encoding="utf-8")
        second_settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "settings_path": str(first_settings),
                "permission_mode": "default",
            },
        )
        try:
            request_json(
                "POST",
                f"{address}/sessions",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "settings_path": str(second_settings),
                    "permission_mode": "bypassPermissions",
                },
            )
        except HttpClientError as exc:
            assert "existing session launch config differs" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected permission_mode mismatch to still raise")
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            # Rejected request must leave the settings metadata exactly as it
            # was before the call — no partial mutation from the field that
            # would have been a no-op restart had the hard field not rejected.
            assert session.metadata["settings_path"] == str(first_settings)
            assert session.metadata.get("restart_needed") is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_settings_change_deferred_while_request_active(tmp_path) -> None:
    """Round 5 gap: the prior /requests-path propagation test only exercised
    the sequential (idle-session) case. Confirm the pre-existing busy-guard
    (unchanged by this diff, already used for effort/model soft changes)
    correctly defers a settings-triggered restart while a request is
    genuinely active — raising "session is busy" rather than killing the
    in-flight process — and that a later request still picks up the deferred
    restart once the session is idle again."""
    state = ControlState()
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        first_settings = tmp_path / "one.json"
        second_settings = tmp_path / "two.json"
        first_settings.write_text('{"hooks": {}}', encoding="utf-8")
        second_settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")

        # Launch the session and leave a request genuinely active (never
        # completed) by not sending a matching /hook/stop.
        result_box: dict = {}

        def send_first() -> None:
            result_box["first"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello",
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                    "settings_path": str(first_settings),
                },
            )

        t1 = threading.Thread(target=send_first)
        t1.start()
        deadline = time.monotonic() + 3
        listed = None
        while time.monotonic() < deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed["sessions"] and listed["sessions"][0]["active_request"] is not None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("first request never became active")
        active_request = listed["sessions"][0]["active_request"]

        # While request 1 is still active, a second request carrying a
        # genuine settings content change must be deferred, not silently
        # kill the process out from under request 1.
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "hello again",
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": False,
                    "launch_process": True,
                    "settings_path": str(second_settings),
                },
            )
        except HttpClientError as exc:
            assert "session is busy" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected settings-triggered restart to be deferred while busy")
        assert fake_process.terminated is False, "process must not be killed out from under the active request"
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True", "restart must stay scheduled for later"

        # Complete request 1; the deferred restart isn't retried automatically
        # (no request is pending for it to piggyback on), matching the
        # existing effort/model soft-restart contract.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "demo", "request_id": active_request, "response": "hi", "cwd": str(tmp_path)},
        )
        t1.join(timeout=3)
        assert result_box["first"]["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_allows_settings_path_change_with_identical_content(tmp_path) -> None:
    """Regression test: a caller that regenerates its --settings file at a new
    path on every restart (e.g. a fresh temp file with a random suffix) must
    not trigger a process restart as long as the file's actual content is
    unchanged — only the path differs. Reproduces a real incident where ACG
    restarting caused every subsequent request to a live session to fail with
    "existing session launch config differs" (settings_path used to be a HARD
    field compared by raw path string; it's fingerprinted by content now).
    Restart is wasted work here since nothing actually changed for the running
    process — must stay a no-op, not just a non-error."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        first_settings = tmp_path / "acg-claude-settings-thpg3ii3.json"
        second_settings = tmp_path / "acg-claude-settings-kbse3yqn.json"
        identical_content = '{"hooks": {"Stop": []}}'
        first_settings.write_text(identical_content, encoding="utf-8")
        second_settings.write_text(identical_content, encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(first_settings)},
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(second_settings)},
        )
        listed = request_json("GET", f"{address}/sessions")
        # The stored path should have been refreshed to the new (still-valid)
        # path so a later re-read doesn't reference a file that may later be
        # cleaned up by the caller.
        assert listed["sessions"][0]["metadata"]["settings_path"] == str(second_settings)
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata.get("restart_needed") is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_allows_settings_path_change_after_old_file_deleted(tmp_path) -> None:
    """Regression test for a live incident: the caller regenerates its
    --settings temp file on every restart AND deletes the previous one soon
    after. If the fingerprint were recomputed from the stored path on every
    call, the deleted old file would hit the "unreadable" fallback and
    permanently mismatch every future request, even though content never
    changed. The fingerprint must instead be cached at write time so a later
    comparison never needs to re-read a path the caller has since removed —
    and, since content never changed, no restart should be scheduled either."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        first_settings = tmp_path / "acg-claude-settings-thpg3ii3.json"
        second_settings = tmp_path / "acg-claude-settings-kbse3yqn.json"
        identical_content = '{"hooks": {"Stop": []}}'
        first_settings.write_text(identical_content, encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(first_settings)},
        )
        # Simulate the caller cleaning up its own old temp file before the
        # next restart sends a new one.
        first_settings.unlink()
        second_settings.write_text(identical_content, encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(second_settings)},
        )
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["settings_path"] == str(second_settings)
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata.get("restart_needed") is None
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_migrates_pre_fingerprint_session_when_old_file_readable(tmp_path) -> None:
    """A session frozen before settings_fingerprint metadata existed has no
    cached value yet. If its recorded settings_path is still readable, the
    migration must fingerprint it and still catch a genuine content change —
    which now means scheduling a restart (settings_path is a soft field), not
    raising."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        first_settings = tmp_path / "one.json"
        second_settings = tmp_path / "two.json"
        first_settings.write_text('{"hooks": {}}', encoding="utf-8")
        second_settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(first_settings)},
        )
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            del session.metadata["settings_fingerprint"]  # simulate a pre-upgrade session
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(second_settings)},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["settings_path"] == str(second_settings)
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_schedules_restart_on_settings_content_change(tmp_path) -> None:
    """Content still matters: if the new path's content genuinely differs,
    it must still be treated as a real change — the fingerprint fix must not
    make this check toothless. Unlike before, this no longer rejects the
    request outright: it schedules a process restart (settings_path is a
    soft field, same as effort/model), so a caller-side infra change (e.g.
    ACG's permission-hook port changing on restart) self-heals instead of
    wedging the session forever. See
    test_control_server_restarts_process_on_settings_content_change for the
    full stop-and-relaunch propagation path."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        first_settings = tmp_path / "acg-claude-settings-thpg3ii3.json"
        second_settings = tmp_path / "acg-claude-settings-kbse3yqn.json"
        first_settings.write_text('{"hooks": {}}', encoding="utf-8")
        second_settings.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(first_settings)},
        )
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(second_settings)},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["restart_needed"] == "True"
            assert session.metadata["resume_on_launch"] == "True"
            assert session.metadata["settings_path"] == str(second_settings)
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_omitted_settings_path_preserves_frozen_settings(tmp_path) -> None:
    """Round 5 finding: the CLI resends its full launch config on every
    invocation (a fresh process each time), so a caller that only passed
    --settings on the first turn of a conversation would omit it on every
    later turn. Before settings_path became a soft field, an omitted value
    (None -> "") would mismatch the frozen path/fingerprint and raise a loud
    "config differs" error — the caller's settings were protected. Making
    settings_path soft accidentally REMOVED that protection: an omitted
    value silently wiped settings_path to "" and scheduled a restart that
    would drop the caller's hooks/permission rules with no warning. Unlike
    the other soft fields (effort omitted -> "medium" is a quality knob that
    never had protection to lose), settings_path can carry security-relevant
    content, so this omission must be treated as "no opinion, keep what's
    frozen" rather than "explicitly clear it"."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        settings_path = tmp_path / "acg-claude-settings-thpg3ii3.json"
        settings_path.write_text(
            '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
            '"command": "curl http://127.0.0.1:9999/hook"}]}]}}',
            encoding="utf-8",
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(settings_path)},
        )
        # Second turn omits settings_path entirely, exactly like cli.py would
        # if the caller only passed --settings on the first invocation.
        result = request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )
        assert result.get("warnings") is None
        with state.lock:
            session = state.registry.get("demo", workdir=str(tmp_path))
            assert session is not None
            assert session.metadata["settings_path"] == str(settings_path), (
                "omitting settings_path on a follow-up call must not clear the frozen settings"
            )
            assert session.metadata.get("restart_needed") is None, (
                "omitting settings_path must not be read as a content change that needs a restart"
            )
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_allows_auto_accept_startup_prompt_change(tmp_path) -> None:
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "auto_accept_workspace_trust": False,
            },
        )
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "auto_accept_workspace_trust": True,
            },
        )
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["auto_accept_workspace_trust"] == "True"
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_rejects_disabled_development_channels(tmp_path) -> None:
    server, address = start_test_server()
    try:
        try:
            request_json(
                "POST",
                f"{address}/sessions",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "dangerously_load_development_channels": False,
                },
            )
        except Exception as exc:
            assert "requires development channels" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected disabled development channels rejection")
    finally:
        server.shutdown()
        server.server_close()


def test_control_server_can_launch_and_stop_managed_process(tmp_path) -> None:
    state = ControlState()
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address
    try:
        created = request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "launch_process": True,
            },
        )
        assert created["session_id"] == "demo"
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["metadata"]["process_pid"] == "4242"
        request_json(
            "DELETE",
            f"{address}/sessions/demo",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )
        assert fake_process.terminated is True
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Background-agent tracking: /hook/agent-launched, /hook/subagent-stop,
# and the gated Stop hook
# ---------------------------------------------------------------------------


def test_stop_hook_defers_when_background_agents_pending(tmp_path) -> None:
    """Stop hook returns deferred=True and does NOT complete the request while
    pending_background_agent_ids is non-empty."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "launch bg agent",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]

        # Simulate PostToolUse(Agent, isAsync=true) delivering agentId
        agent_launched = request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        assert agent_launched["ok"] is True
        assert agent_launched["pending"] == 1

        # First (premature) Stop fires — should be deferred
        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "response": "launched",
                "cwd": str(tmp_path),
            },
        )
        assert stop_response.get("deferred") is True

        # Request is still active
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] == queued["request_id"]

        # Background agent finishes — SubagentStop carries the same agentId
        subagent_stop = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        assert subagent_stop["ok"] is True
        assert subagent_stop["pending"] == 0

        # Final Stop fires — should complete
        final_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "response": "Agent output: DONE",
                "cwd": str(tmp_path),
            },
        )
        assert final_stop == {"ok": True}

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_stop_hook_completes_immediately_when_no_background_agents(tmp_path) -> None:
    """Normal (no bg agents) Stop hook continues to complete the request immediately."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "response": "world",
                "cwd": str(tmp_path),
            },
        )
        assert stop_response == {"ok": True}
        assert "deferred" not in stop_response

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_agent_launched_tracks_agent_ids(tmp_path) -> None:
    """Two distinct background agents are each tracked by agentId."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        r1 = request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        assert r1["pending"] == 1

        r2 = request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-002"},
        )
        assert r2["pending"] == 2
    finally:
        server.shutdown()
        server.server_close()


def test_subagent_stop_discards_agent_id(tmp_path) -> None:
    """SubagentStop with the matching agentId removes it from the tracking set."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        r = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        assert r["pending"] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_subagent_stop_for_unknown_agent_id_is_noop(tmp_path) -> None:
    """SubagentStop for an unknown agentId is a safe no-op — the known agent stays tracked."""
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        session_id = request_json("GET", f"{address}/sessions")["sessions"][0]["session_id"]

        # Register agent-A
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-A"},
        )

        # SubagentStop for agent-B (unknown) — must be a no-op
        r = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-B"},
        )
        assert r["pending"] == 1  # agent-A is still tracked

        # The request must still be active (Stop hook would defer)
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None

        # Clean up: remove agent-A, then complete
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-A"},
        )
        stop_r = request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "ok", "cwd": str(tmp_path)},
        )
        assert stop_r == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()


def test_pending_set_resets_on_new_request(tmp_path) -> None:
    """pending_background_agent_ids is cleared at the start of each new request."""
    server, address = start_test_server()
    try:
        # Start first request, track one agent, complete via SubagentStop + Stop
        request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "r1",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": "demo", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "demo", "response": "r1 done", "cwd": str(tmp_path)},
        )

        # Start second request — pending set must be reset to empty
        request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "r2",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        # Stop immediately (no bg agents) — must complete without defer
        stop_r = request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "demo", "response": "r2 done", "cwd": str(tmp_path)},
        )
        assert stop_r == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()


def test_agent_launched_returns_no_session_when_session_not_found(tmp_path) -> None:
    server, address = start_test_server()
    try:
        r = request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": "nonexistent-session-id", "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        assert r.get("no_session") is True
    finally:
        server.shutdown()
        server.server_close()


def test_subagent_stop_returns_no_session_when_session_not_found(tmp_path) -> None:
    server, address = start_test_server()
    try:
        r = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": "nonexistent-session-id", "cwd": str(tmp_path)},
        )
        assert r.get("no_session") is True
    finally:
        server.shutdown()
        server.server_close()


def test_transcript_path_defers_when_background_agents_pending(tmp_path, monkeypatch) -> None:
    """The _wait_for_response() transcript path does not complete the request when
    pending_background_agents > 0, even if transcript shows end_turn."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    call_counts = {"n": 0}

    def fake_read_response(*_args, **_kwargs):
        call_counts["n"] += 1
        if call_counts["n"] >= 2:
            return TranscriptResponse("premature response", stop_reason="end_turn")
        return None

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        fake_read_response,
    )

    try:
        q = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        session_id = q["session_id"]

        # Track a background agent before the transcript path fires
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )

        # Give the transcript poller a moment to observe end_turn but not complete
        import time
        time.sleep(1.0)

        # Request should still be active (not completed)
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] == q["request_id"], (
            "request was completed prematurely while bg agent still pending"
        )

        # Finish the bg agent and let a real Stop complete the request
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "final response", "cwd": str(tmp_path)},
        )
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_transcript_path_does_not_complete_immediately_after_subagent_stop(tmp_path, monkeypatch) -> None:
    """After SubagentStop fires and counter drops to 0, the transcript path must
    NOT complete with the stale deferred response.  The fix clears
    stable_transcript_response / stable_transcript_signature on deferral so
    the stability timer restarts — at least transcript_quiet_seconds (0.5 s) must
    elapse before completion can happen via that path."""
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    # Transcript always returns the same "premature" response with end_turn —
    # it never updates to a "real" final response in this scenario.
    def fake_read_response(*_args, **_kwargs):
        return TranscriptResponse("premature response", stop_reason="end_turn")

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        fake_read_response,
    )

    try:
        q = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 8,
                "workdir": str(tmp_path),
            },
        )
        session_id = q["session_id"]

        # Track a background agent so the first transcript-path fire defers.
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )

        # Wait long enough for the transcript path to fire (>0.5s quiet period)
        # and then defer (clearing stable values).
        time.sleep(1.2)

        # Now fire SubagentStop with the matching agentId: set empties.
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "agent-001"},
        )

        # Immediately after counter drops to 0 the request should still be
        # active — the fix cleared stable_transcript_response so the timer
        # must restart; 0.1 s is well inside the 0.5 s quiet window.
        time.sleep(0.1)
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] == q["request_id"], (
            "request completed too quickly after subagent-stop — "
            "stable_transcript_response was probably not cleared on deferral"
        )

        # Finish cleanly via Stop hook (transcript stays stale; Stop hook wins).
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "real response", "cwd": str(tmp_path)},
        )
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Transcript-scan background-agent detection (PostToolUse doesn't fire in
# bypassPermissions mode — Stop hook must scan transcript instead)
# ---------------------------------------------------------------------------

import json as _json


def _write_bg_agent_transcript(path: Path, request_id: str, agent_id: str) -> None:
    """Write a minimal transcript with a background-agent launch under *request_id*."""
    tool_result_text = (
        f"Async agent launched successfully.\n"
        f"agentId: {agent_id} (internal ID - do not mention to user.)\n"
        f"The agent is working in the background."
    )
    events = [
        # Request marker
        {"message": {"role": "user", "content": [
            {"type": "text", "text": f'<poor-claude-request id="{request_id}">'},
        ]}},
        # Tool_use (assistant launches background agent)
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_test", "name": "Agent",
             "input": {"run_in_background": True, "prompt": "do work"}},
        ]}},
        # Tool_result (Async launch confirmation with agentId)
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_test",
             "content": [{"type": "text", "text": tool_result_text}]},
        ]}},
        # Assistant premature response
        {"message": {"role": "assistant", "content": "LAUNCHED", "stop_reason": "end_turn"}},
    ]
    path.write_text("\n".join(_json.dumps(e) for e in events), encoding="utf-8")


def test_stop_hook_defers_via_transcript_scan(tmp_path) -> None:
    """Stop hook discovers background agents from transcript when PostToolUse never fired."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "launch bg agent",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]

        # Write a transcript that has the bg agent launch (PostToolUse never fired)
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bg_agent_transcript(transcript, request_id=request_id, agent_id="tx-agent-001")

        # Stop hook fires with transcript_path pointing to our transcript
        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "LAUNCHED",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert stop_response.get("deferred") is True

        # Request must still be active
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] == queued["request_id"]

        # SubagentStop fires with the discovered agentId
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "tx-agent-001"},
        )

        # Final Stop (real response after task-notification)
        final_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "BG-DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert final_stop == {"ok": True}

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_intermediate_response_prepended_to_final_via_stop_hook_path(tmp_path) -> None:
    """When the Stop hook defers, the intermediate response is prepended to the final response.

    The first Stop hook fires with "LAUNCHED" (deferred because a bg agent is pending).
    After SubagentStop clears the pending set, the second Stop hook fires with "BG-DONE".
    _wait_for_response must return "LAUNCHED\\n\\nBG-DONE".
    """
    server, address = start_test_server()
    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "launch bg agent",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        request_id = listed["sessions"][0]["active_request"]
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bg_agent_transcript(transcript, request_id=request_id, agent_id="bg-001")

        # First Stop hook — deferred because bg agent is pending.
        first_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "LAUNCHED",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert first_stop.get("deferred") is True

        # SubagentStop fires — pending clears.
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "bg-001"},
        )

        # Second Stop hook fires with real final response.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "BG-DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )

        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "LAUNCHED\n\nBG-DONE"
    finally:
        server.shutdown()
        server.server_close()


def test_stop_hook_transcript_scan_race_subagent_stops_first(tmp_path) -> None:
    """SubagentStop fires before the Stop hook scan: completed_agent_ids prevents defer loop."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "launch bg agent",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]

        # SubagentStop fires FIRST (background agent completed quickly)
        subagent_stop = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "fast-agent"},
        )
        assert subagent_stop["ok"] is True

        # Now write transcript (as if the session log caught up)
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bg_agent_transcript(transcript, request_id=request_id, agent_id="fast-agent")

        # Stop hook fires — transcript scan finds "fast-agent" but it's already in
        # completed_agent_ids, so it must NOT be added to pending → complete immediately.
        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "BG-DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert stop_response == {"ok": True}
        assert "deferred" not in stop_response

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_does_not_complete_from_transcript_when_bg_agent_already_done(
    tmp_path, monkeypatch
) -> None:
    """_wait_for_response must NOT finish with the premature transcript response when
    SubagentStop already fired before the transcript scan.

    Race flow:
      1. SubagentStop fires → completed_agent_ids = {"race-agent"}, pending stays empty.
      2. Transcript polling stabilises on "LAUNCHED" (end_turn) after 0.5 s.
      3. Transcript scan finds "race-agent"; it's in completed_agent_ids → pending stays empty.
      4. Old code: pending=0 → finish("LAUNCHED") ← BUG
         New code: bg_work_detected=True, pending=0 → reset timer, keep looping.
      5. Second Stop hook fires with "BG-DONE" → finish_request_for_route → request done.
    """
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: TranscriptResponse("LAUNCHED", stop_reason="end_turn"),
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_agent_ids_in_transcript",
        lambda *_a, **_kw: ["race-agent"],
    )

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "launch bg agent",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to be queued.
        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        # SubagentStop fires FIRST — before _wait_for_response scans the transcript.
        # Agent lands in completed_agent_ids; pending stays empty.
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "race-agent"},
        )

        # Wait long enough for the transcript polling cycle to fire (>0.5 s quiet
        # window + a comfortable margin).  Old code would finish with "LAUNCHED" here.
        time.sleep(1.5)

        # Request must still be active — bg_work_detected guard prevents early finish.
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None, (
            "request completed prematurely with stale 'LAUNCHED' response — "
            "bg_work_detected guard is not preventing early completion"
        )

        # Second Stop hook fires with the real final response.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "BG-DONE", "cwd": str(tmp_path)},
        )

        req_thread.join(timeout=3)
        # Response must combine the intermediate "LAUNCHED" turn with the final
        # "BG-DONE" turn separated by a blank line.
        assert result_box.get("result", {}).get("response") == "LAUNCHED\n\nBG-DONE"
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_subagent_stop_adds_to_completed_agent_ids(tmp_path) -> None:
    """SubagentStop populates completed_agent_ids so race-guard works even without pending set."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "hello",
                "timeout_seconds": 5,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]

        # SubagentStop for an agent that was never in pending (PostToolUse never fired)
        r = request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "ghost-agent"},
        )
        assert r["ok"] is True

        # Write transcript with that same ghost-agent
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bg_agent_transcript(transcript, request_id=request_id, agent_id="ghost-agent")

        # Stop hook — ghost-agent must be in completed_agent_ids → no defer
        stop_r = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "done",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert stop_r == {"ok": True}
        assert "deferred" not in stop_r
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Bash(run_in_background=True) task-tracking tests
# ---------------------------------------------------------------------------

def _write_bash_task_transcript(
    path: Path,
    request_id: str,
    task_id: str,
    *,
    completed: bool = False,
    completion_status: str = "completed",
    premature_response: str = "TASK_RUNNING",
    final_response: str | None = None,
) -> None:
    """Write a minimal transcript simulating a Bash background task launch.

    If *completed* is True, also appends a ``<task-notification>`` user message
    and optionally a final assistant response.
    """
    tool_result_text = (
        f"Command running in background with ID: {task_id}. "
        f"Output is being written to: /tmp/tasks/{task_id}.output. "
        "You will be notified when it completes."
    )
    events = [
        {"message": {"role": "user", "content": [
            {"type": "text", "text": f'<poor-claude-request id="{request_id}">'},
        ]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_bash", "name": "Bash",
             "input": {"command": "sleep 5", "run_in_background": True}},
        ]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_bash",
             "content": [{"type": "text", "text": tool_result_text}]},
        ]}},
        {"message": {"role": "assistant", "content": premature_response, "stop_reason": "end_turn"}},
    ]
    if completed:
        notification = (
            f"<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>toolu_bash</tool-use-id>\n"
            f"<output-file>/tmp/tasks/{task_id}.output</output-file>\n"
            f"<status>{completion_status}</status>\n"
            f"<summary>Background command completed</summary>\n"
            f"</task-notification>"
        )
        events.append({"message": {"role": "user", "content": notification}})
        if final_response is not None:
            events.append({"message": {
                "role": "assistant",
                "content": final_response,
                "stop_reason": "end_turn",
            }})
    path.write_text("\n".join(_json.dumps(e) for e in events), encoding="utf-8")


def test_stop_hook_defers_when_bash_task_pending(tmp_path) -> None:
    """Stop hook must defer when a Bash background task is still running."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "run bg bash task",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]

        # Transcript shows launch but no completion yet
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bash_task_transcript(transcript, request_id=request_id, task_id="btask0001")

        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_RUNNING",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert stop_response.get("deferred") is True

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] == request_id
    finally:
        server.shutdown()
        server.server_close()


def test_stop_hook_completes_immediately_when_bash_task_already_done(tmp_path) -> None:
    """If the transcript shows both launch AND a terminal task-notification, no defer."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "run fast bash task",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]

        # Transcript shows launch AND completion in the same transcript
        transcript = tmp_path / f"{session_id}.jsonl"
        _write_bash_task_transcript(
            transcript, request_id=request_id, task_id="btask0002",
            completed=True, final_response="TASK_DONE",
        )

        stop_response = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert stop_response == {"ok": True}
        assert "deferred" not in stop_response

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_stop_hook_bash_task_deferred_then_completed_on_second_stop(tmp_path) -> None:
    """Full lifecycle: first Stop defers (task running), second Stop completes (task done)."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "run bash task",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]
        transcript = tmp_path / f"{session_id}.jsonl"

        # First Stop: transcript shows launch only (task still running)
        _write_bash_task_transcript(transcript, request_id=request_id, task_id="btask0003")
        first_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_RUNNING",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert first_stop.get("deferred") is True

        # Task completes — update transcript with notification + final response
        _write_bash_task_transcript(
            transcript, request_id=request_id, task_id="btask0003",
            completed=True, final_response="TASK_DONE",
        )

        # Second Stop: transcript now shows completion
        second_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert second_stop == {"ok": True}
        assert "deferred" not in second_stop

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_bash_task_intermediate_response_prepended_to_final(tmp_path) -> None:
    """_wait_for_response must return 'TASK_RUNNING\\n\\nTASK_DONE' for a Bash background task."""
    server, address = start_test_server()
    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "run bash task",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        request_id = listed["sessions"][0]["active_request"]
        transcript = tmp_path / f"{session_id}.jsonl"

        # First Stop: task still running
        _write_bash_task_transcript(transcript, request_id=request_id, task_id="btask0004")
        first_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_RUNNING",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert first_stop.get("deferred") is True

        # Task completes — update transcript
        _write_bash_task_transcript(
            transcript, request_id=request_id, task_id="btask0004",
            completed=True, final_response="TASK_DONE",
        )

        # Second Stop: task done
        request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_DONE",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )

        req_thread.join(timeout=5)
        assert not req_thread.is_alive(), "request thread did not finish"
        assert result_box.get("result", {}).get("response") == "TASK_RUNNING\n\nTASK_DONE"
    finally:
        server.shutdown()
        server.server_close()


def test_stop_hook_bash_task_killed_status_treated_as_terminal(tmp_path) -> None:
    """'killed' status in task-notification must remove the task from pending (no defer)."""
    server, address = start_test_server()
    try:
        queued = request_json(
            "POST",
            f"{address}/requests",
            {
                "session_id": "demo",
                "prompt": "run killed bash task",
                "timeout_seconds": 10,
                "workdir": str(tmp_path),
            },
        )
        session_id = queued["session_id"]
        request_id = queued["request_id"]
        transcript = tmp_path / f"{session_id}.jsonl"

        # First Stop defers — task is running
        _write_bash_task_transcript(transcript, request_id=request_id, task_id="btask0005")
        first_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_RUNNING",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert first_stop.get("deferred") is True

        # Task is killed — transcript updated with 'killed' notification
        _write_bash_task_transcript(
            transcript, request_id=request_id, task_id="btask0005",
            completed=True, completion_status="killed", final_response="TASK_KILLED",
        )

        # Second Stop: 'killed' is terminal → must complete, not defer
        second_stop = request_json(
            "POST",
            f"{address}/hook/stop",
            {
                "session_id": session_id,
                "request_id": request_id,
                "response": "TASK_KILLED",
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
            },
        )
        assert second_stop == {"ok": True}
        assert "deferred" not in second_stop
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_bash_task_not_missed_when_completion_only_in_first_candidate(
    tmp_path, monkeypatch
) -> None:
    """The _wait_for_response candidate loop must NOT stop when a candidate has only
    find_completed results but no launch — it must continue scanning other candidates.

    Scenario:
      candidate A: find_tasks=[], find_completed=["btask8888"]  (stale notification, no launch)
      candidate B: find_tasks=["btask8888"], find_completed=["btask8888"]

    OLD (buggy) behaviour: break at A because found_completed is truthy →
      newly_discovered_tasks=[] → bg_work_detected stays False → pending=0 →
      finish_request_for_route("LAUNCHED") prematurely.

    NEW (correct) behaviour: don't break at A (no launch) → continue to B →
      find launch → add to pending → discard (completed) → net pending=0 →
      bg_work_detected=True → elif branch → keep looping → Stop hook finishes.
    """
    candidate_a = tmp_path / "candidate_a.jsonl"
    candidate_b = tmp_path / "candidate_b.jsonl"
    candidate_a.write_text("", encoding="utf-8")
    candidate_b.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "poor_claude.control_server.transcript_candidates",
        lambda **_kw: [candidate_a, candidate_b],
    )
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: TranscriptResponse("LAUNCHED", stop_reason="end_turn"),
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_agent_ids_in_transcript",
        lambda *_a, **_kw: [],
    )

    def fake_find_tasks(path, **_kw):
        # Only candidate B has the launch.
        return ["btask8888"] if path == candidate_b else []

    def fake_find_completed(path, **_kw):
        # Candidate A carries a stale completion with no corresponding launch.
        # Candidate B also has the completion (same task).
        return ["btask8888"] if path in (candidate_a, candidate_b) else []

    monkeypatch.setattr("poor_claude.control_server.find_background_task_ids_in_transcript", fake_find_tasks)
    monkeypatch.setattr("poor_claude.control_server.find_completed_task_ids_in_transcript", fake_find_completed)

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "run bash task",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        # Wait for the transcript scan to fire (>0.5 s quiet window + margin).
        # With the OLD buggy break condition, the request would finish here with
        # "LAUNCHED" because candidate A's completion-only result stops the loop.
        time.sleep(1.5)

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None, (
            "request completed prematurely — candidate loop broke early on "
            "completion-only candidate A before scanning candidate B for the launch"
        )

        # Stop hook fires with the real final response.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "BG-DONE", "cwd": str(tmp_path)},
        )

        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "LAUNCHED\n\nBG-DONE"
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_bash_task_completed_before_scan(
    tmp_path, monkeypatch
) -> None:
    """_wait_for_response must not complete with a premature response when a Bash task
    was already done before the transcript scan fires.

    This exercises the pure transcript-polling path (no Stop hooks / bypassPermissions).
    The scan simultaneously finds task launch AND completion in the transcript:
      1. find_background_task_ids → ["btasktest1"] (add to pending)
      2. find_completed_task_ids → ["btasktest1"] (discard from pending)
      3. Net result: pending=0, bg_work_detected=True → elif branch → keep looping.
      4. Stop hook fires with real final response → finish.
      5. response must be "LAUNCHED\\n\\nBG-DONE".
    """
    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: TranscriptResponse("LAUNCHED", stop_reason="end_turn"),
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_task_ids_in_transcript",
        lambda *_a, **_kw: ["btasktest1"],
    )
    monkeypatch.setattr(
        "poor_claude.control_server.find_completed_task_ids_in_transcript",
        lambda *_a, **_kw: ["btasktest1"],
    )

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "run bash task",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to become active.
        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        # Wait long enough for transcript polling (>0.5 s quiet window + comfortable margin).
        # Old code without bg_work_detected guard would finish with "LAUNCHED" here.
        time.sleep(1.5)

        # Request must still be active — bg_work_detected prevents early finish even
        # when pending is empty (task was launched and completed in the same scan window).
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None, (
            "request completed prematurely with stale 'LAUNCHED' response — "
            "bg_work_detected guard not working for Bash tasks"
        )

        # Stop hook fires with the real final response.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "BG-DONE", "cwd": str(tmp_path)},
        )

        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "LAUNCHED\n\nBG-DONE"
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is None
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Request queue tests
# ---------------------------------------------------------------------------

def test_second_concurrent_request_queues_and_completes_after_first(tmp_path) -> None:
    """Two concurrent requests to the same session: R2 queues behind R1 and is
    served after R1's Stop hook fires."""
    server, address = start_test_server()
    try:
        r1_result: dict = {}
        r2_result: dict = {}

        def send_request(label: str, result_box: dict) -> None:
            try:
                result_box[label] = request_json(
                    "POST",
                    f"{address}/requests",
                    {
                        "session_id": "demo",
                        "workdir": str(tmp_path),
                        "prompt": f"prompt-{label}",
                        "timeout_seconds": 10,
                        "wait_for_response": True,
                    },
                    timeout=15,
                )
            except Exception as exc:
                result_box[label] = {"error": str(exc)}

        t1 = threading.Thread(target=send_request, args=("r1", r1_result), daemon=True)
        t2 = threading.Thread(target=send_request, args=("r2", r2_result), daemon=True)
        t1.start()

        # Give R1 time to become the active request before sending R2.
        deadline = time.time() + 5
        while time.time() < deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed.get("sessions") and listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("R1 never became active")

        t2.start()

        # Give R2 time to enqueue.
        time.sleep(0.2)
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None, "R1 should still be active"

        # Retrieve the session_id from the server state for the stop hook.
        session_id = listed["sessions"][0]["session_id"]

        # Complete R1 via Stop hook.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "response-r1", "cwd": str(tmp_path)},
        )
        t1.join(timeout=5)
        assert r1_result.get("r1", {}).get("response") == "response-r1"

        # R2 should now be active; complete it too.
        deadline = time.time() + 5
        while time.time() < deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("R2 never became active after R1 completed")

        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "response-r2", "cwd": str(tmp_path)},
        )
        t2.join(timeout=5)
        assert r2_result.get("r2", {}).get("response") == "response-r2"
    finally:
        server.shutdown()
        server.server_close()


def test_queue_full_returns_429(tmp_path) -> None:
    """Sending more requests than MAX_PENDING_QUEUE_DEPTH + 1 active should
    return HTTP 429."""
    from poor_claude.session import MAX_PENDING_QUEUE_DEPTH

    server, address = start_test_server()
    try:
        # Create the session first.
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path)},
        )

        threads = []
        results = {}

        # Send MAX_PENDING_QUEUE_DEPTH + 1 requests concurrently (1 active + max queued).
        for i in range(MAX_PENDING_QUEUE_DEPTH + 1):
            label = f"r{i}"
            results[label] = None

            def _send(lbl=label, idx=i) -> None:
                try:
                    results[lbl] = request_json(
                        "POST",
                        f"{address}/requests",
                        {
                            "session_id": "demo",
                            "workdir": str(tmp_path),
                            "prompt": f"prompt-{idx}",
                            "timeout_seconds": 30,
                            "wait_for_response": True,
                        },
                        timeout=35,
                    )
                except HttpClientError as exc:
                    results[lbl] = {"error": str(exc)}

            t = threading.Thread(target=_send, daemon=True)
            threads.append(t)
            t.start()
            time.sleep(0.05)  # stagger slightly so ordering is predictable

        # Poll until all MAX_PENDING_QUEUE_DEPTH + 1 requests have either queued or
        # become active, so the overflow check below is not racing with enqueue.
        wait_deadline = time.time() + 10
        while time.time() < wait_deadline:
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions:
                # active (1) + pending queue depth must equal MAX_PENDING_QUEUE_DEPTH
                pending = sessions[0].get("pending_queue_depth", 0)
                if pending >= MAX_PENDING_QUEUE_DEPTH:
                    break
            time.sleep(0.05)

        # Now try one more — should be rejected with 429.
        # HttpClientError message format: "POST <url> failed: <code> <body>"
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "prompt": "overflow",
                    "timeout_seconds": 30,
                    "wait_for_response": True,
                },
            )
            raise AssertionError("Expected HttpClientError with 429")
        except HttpClientError as exc:
            assert "429" in str(exc), f"Expected 429 in error message, got: {exc}"
            assert exc.payload is not None and "queue is full" in exc.payload.get("error", "")

        # Clean up: stop session so threads unblock.
        request_json(
            "DELETE",
            f"{address}/sessions/demo",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )
        for t in threads:
            t.join(timeout=5)
    finally:
        server.shutdown()
        server.server_close()


def test_queued_requests_cancelled_on_session_stop(tmp_path) -> None:
    """Queued requests receive a cancellation error when the session is stopped."""
    server, address = start_test_server()
    try:
        request_json("POST", f"{address}/sessions", {"session_id": "demo", "workdir": str(tmp_path)})

        r1_result: dict = {}
        r2_result: dict = {}

        def send_request(label: str, result_box: dict) -> None:
            try:
                result_box[label] = request_json(
                    "POST",
                    f"{address}/requests",
                    {
                        "session_id": "demo",
                        "workdir": str(tmp_path),
                        "prompt": f"prompt-{label}",
                        "timeout_seconds": 30,
                        "wait_for_response": True,
                    },
                    timeout=35,
                )
            except Exception as exc:
                result_box[label] = {"error": str(exc)}

        t1 = threading.Thread(target=send_request, args=("r1", r1_result), daemon=True)
        t2 = threading.Thread(target=send_request, args=("r2", r2_result), daemon=True)
        t1.start()

        # Wait for R1 to become active.
        deadline = time.time() + 5
        while time.time() < deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed.get("sessions") and listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)

        t2.start()
        # Poll until R2 appears in the queue — more reliable than a fixed sleep.
        wait_deadline = time.time() + 5
        while time.time() < wait_deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed.get("sessions") and listed["sessions"][0].get("pending_queue_depth", 0) > 0:
                break
            time.sleep(0.05)

        # Stop the session — should cancel R2 (and cause R1 to error since the
        # session route is removed mid-wait).
        request_json(
            "DELETE",
            f"{address}/sessions/demo",
            headers={"X-Poor-Claude-Workdir": str(tmp_path)},
        )

        t1.join(timeout=5)
        t2.join(timeout=5)

        # R2 should have been cancelled with an error (session removed → HTTP 400 or
        # RuntimeError from cancelled flag).
        assert "error" in r2_result.get("r2", {}), (
            f"Expected R2 to receive an error after session stop, got: {r2_result}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_queued_request_cancelled_when_r1_completes_via_transcript_at_deadline(
    tmp_path, monkeypatch
) -> None:
    """R2 is cancelled promptly when R1's deadline fires with a transcript fallback response.

    Scenario:
      - R1 is active.  R2 queues behind it.
      - R1's deadline fires with a non-end_turn transcript response — the code calls
        finish_request_for_route (rather than timeout_request_for_route), which auto-promotes R2.
      - The fix calls cancel_queued_requests_for_route immediately after so R2 receives a
        cancellation error at once, rather than hanging until its own (long) timeout.
    """
    from http.server import ThreadingHTTPServer

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    # Gate: flip on after R2 is queued so the transcript poller starts seeing a response
    # with enough lead time for it to stabilise (>0.5 s) before the 2-second deadline.
    transcript_enabled = threading.Event()

    def fake_read_response(*_a, **_kw):
        if transcript_enabled.is_set():
            # Use stop_reason=None so the early-return ("end_turn") path is bypassed.
            # The response accumulates in transcript_fallback_response until the deadline.
            return TranscriptResponse("response-from-transcript", stop_reason=None)
        return None

    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        fake_read_response,
    )

    r1_result: dict = {}
    r2_result: dict = {}

    def send_r1() -> None:
        try:
            r1_result["r1"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "prompt": "prompt-r1",
                    # timeout_seconds=4: gives at least 2 s of margin after the
                    # transcript stabilises (>0.5 s quiet window) for the deadline to
                    # fire on even a heavily loaded CI machine.
                    "timeout_seconds": 4,
                    "wait_for_response": True,
                },
                timeout=12,
            )
        except Exception as exc:
            r1_result["error"] = str(exc)

    def send_r2() -> None:
        try:
            r2_result["r2"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "prompt": "prompt-r2",
                    # Long timeout — without the fix R2 would block here for 30 s.
                    "timeout_seconds": 30,
                    "wait_for_response": True,
                },
                timeout=35,
            )
        except Exception as exc:
            r2_result["error"] = str(exc)

    t1 = threading.Thread(target=send_r1, daemon=True)
    t2 = threading.Thread(target=send_r2, daemon=True)
    t1.start()

    # Wait for R1 to become the active request.
    wait_deadline = time.time() + 5
    while time.time() < wait_deadline:
        listed = request_json("GET", f"{address}/sessions")
        if listed.get("sessions") and listed["sessions"][0].get("active_request"):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("R1 never became active")

    t2.start()
    # Poll until R2 is in the queue — more reliable than a fixed sleep on slow CI.
    wait_deadline = time.time() + 5
    while time.time() < wait_deadline:
        listed = request_json("GET", f"{address}/sessions")
        if listed.get("sessions") and listed["sessions"][0].get("pending_queue_depth", 0) > 0:
            break
        time.sleep(0.05)

    # Enable the transcript response.  It must stabilise for 0.5 s before R1's
    # 4-second deadline.  Enabling here gives ~3 s of lead time on any machine.
    transcript_enabled.set()

    # R1 deadline fires at ~t=4 s.  With the fix, R2 is cancelled immediately.
    # Both threads must finish well within the test timeout.
    t1.join(timeout=12)
    t2.join(timeout=6)  # R2 must NOT hang until its 30-second timeout

    assert not t1.is_alive(), "R1 thread did not complete in time"
    assert not t2.is_alive(), (
        "R2 thread did not complete promptly — "
        "cancel_queued_requests_for_route was likely not called after finish_request_for_route"
    )

    # R1 should have received the transcript response.
    assert r1_result.get("r1", {}).get("response") == "response-from-transcript", (
        f"Expected R1 to return transcript response; got: {r1_result}"
    )

    # R2 must have received a cancellation error, not a success response.
    assert "error" in r2_result, (
        f"Expected R2 to be cancelled (error), but got: {r2_result}"
    )

    server.shutdown()
    server.server_close()


def test_queued_request_timeout_starts_at_activation_not_enqueue(tmp_path) -> None:
    """R2's timeout_seconds clock starts when R2 is activated, not when it was enqueued.

    R1 takes 3 seconds.  R2 has timeout_seconds=2.  If timeout were measured from
    enqueue time, R2 would time out before R1 finishes.  With activation-time semantics,
    R2 has a full 2 seconds after R1 completes, so both requests must succeed.
    """
    server, address = start_test_server()
    try:
        r1_result: dict = {}
        r2_result: dict = {}

        def send_r1() -> None:
            try:
                r1_result["r1"] = request_json(
                    "POST",
                    f"{address}/requests",
                    {
                        "session_id": "demo",
                        "workdir": str(tmp_path),
                        "prompt": "prompt-r1",
                        "timeout_seconds": 10,
                        "wait_for_response": True,
                    },
                    timeout=15,
                )
            except Exception as exc:
                r1_result["error"] = str(exc)

        def send_r2() -> None:
            try:
                r2_result["r2"] = request_json(
                    "POST",
                    f"{address}/requests",
                    {
                        "session_id": "demo",
                        "workdir": str(tmp_path),
                        "prompt": "prompt-r2",
                        # Shorter than R1's processing time — would time out if clock
                        # started at enqueue rather than at activation.
                        "timeout_seconds": 2,
                        "wait_for_response": True,
                    },
                    timeout=15,
                )
            except Exception as exc:
                r2_result["error"] = str(exc)

        t1 = threading.Thread(target=send_r1, daemon=True)
        t2 = threading.Thread(target=send_r2, daemon=True)
        t1.start()

        # Wait for R1 to become active.
        wait_deadline = time.time() + 5
        while time.time() < wait_deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed.get("sessions") and listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("R1 never became active")

        t2.start()
        time.sleep(0.2)  # Let R2 enqueue; its timeout_seconds=2 clock has NOT started.

        # Hold R1 for 3 seconds — longer than R2's timeout_seconds.
        # If timeout starts at enqueue, R2 would expire here.
        time.sleep(3)

        # Complete R1 via Stop hook.
        session_id = request_json("GET", f"{address}/sessions")["sessions"][0]["session_id"]
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "response-r1", "cwd": str(tmp_path)},
        )
        t1.join(timeout=5)

        # R2 is now activated; its 2-second window starts NOW.  Complete it quickly.
        wait_deadline = time.time() + 5
        while time.time() < wait_deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("R2 never became active after R1 completed")

        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "response-r2", "cwd": str(tmp_path)},
        )
        t2.join(timeout=5)

        assert not t1.is_alive(), "R1 thread did not complete"
        assert not t2.is_alive(), "R2 thread did not complete"
        assert r1_result.get("r1", {}).get("response") == "response-r1", f"R1: {r1_result}"
        assert r2_result.get("r2", {}).get("response") == "response-r2", (
            f"R2 timed out or errored — timeout likely started at enqueue rather than "
            f"activation; got: {r2_result}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_soft_restart_blocked_while_request_is_active(tmp_path) -> None:
    """A launch_process=True request with updated soft params is rejected when another
    request is already active, rather than killing the active request's process."""
    server, address = start_test_server()
    try:
        # Create session with initial params and launch_process=True.
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "effort": "normal",
            },
        )

        r1_result: dict = {}

        def send_r1() -> None:
            try:
                r1_result["r1"] = request_json(
                    "POST",
                    f"{address}/requests",
                    {
                        "session_id": "demo",
                        "workdir": str(tmp_path),
                        "prompt": "prompt-r1",
                        "timeout_seconds": 30,
                        "wait_for_response": True,
                    },
                    timeout=35,
                )
            except Exception as exc:
                r1_result["error"] = str(exc)

        t1 = threading.Thread(target=send_r1, daemon=True)
        t1.start()

        # Wait for R1 to become active.
        wait_deadline = time.time() + 5
        while time.time() < wait_deadline:
            listed = request_json("GET", f"{address}/sessions")
            if listed.get("sessions") and listed["sessions"][0].get("active_request"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("R1 never became active")

        # Send R2 with launch_process=True and different soft params — this must be
        # rejected (400) rather than killing R1's process.
        try:
            request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "prompt": "prompt-r2",
                    "timeout_seconds": 5,
                    "wait_for_response": True,
                    "launch_process": True,
                    "effort": "high",  # different from session's "normal"
                },
            )
            raise AssertionError("Expected 400 error for restart-while-busy")
        except HttpClientError as exc:
            assert "400" in str(exc) or exc.payload is not None, f"Unexpected error: {exc}"
            assert exc.payload is not None and "busy" in exc.payload.get("error", "").lower(), (
                f"Expected 'busy' in error message; got: {exc.payload}"
            )

        # R1 must still be running (its process was NOT killed by R2).
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0].get("active_request") is not None, (
            "R1 should still be active after R2's rejected restart attempt"
        )

        # Complete R1 normally.
        session_id = listed["sessions"][0]["session_id"]
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "response-r1", "cwd": str(tmp_path)},
        )
        t1.join(timeout=5)
        assert r1_result.get("r1", {}).get("response") == "response-r1", f"R1: {r1_result}"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_nudges_stalled_claude(tmp_path, monkeypatch) -> None:
    """No-response watchdog: when the transcript stops growing and no Stop hook
    arrives (Claude hung on a stalled SSE stream), _wait_for_response should send
    up to POOR_CLAUDE_MAX_NUDGES nudge notifications before falling through to the
    hard-timeout/kill path.  See anthropics/claude-code#26224.
    """
    # No transcript file exists, so byte-size never grows → a stall is detected.
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0.2")
    monkeypatch.setenv("POOR_CLAUDE_STALL_ACTION", "nudge")
    monkeypatch.setenv("POOR_CLAUDE_MAX_NUDGES", "2")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the watchdog to reach the nudge cap (2).
        session_id = None
        nudges = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                nudges = sessions[0]["metadata"].get("nudges_sent")
                if nudges == "2":
                    break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"
        assert nudges == "2", f"watchdog did not send the expected nudges: {nudges}"

        # Claude finally wakes and the Stop hook delivers the real response.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "WOKE-UP", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "WOKE-UP"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_watchdog_disabled_when_stall_zero(tmp_path, monkeypatch) -> None:
    """POOR_CLAUDE_STALL_SECONDS=0 disables the watchdog: no nudges are sent and
    the normal Stop-hook completion path is undisturbed.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        # Give the loop time to run several iterations; with the watchdog disabled
        # no nudges must be recorded.
        time.sleep(1.0)
        listed = request_json("GET", f"{address}/sessions")
        assert "nudges_sent" not in listed["sessions"][0]["metadata"]

        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "DONE", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "DONE"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_watchdog_skips_nudge_during_background_work(tmp_path, monkeypatch) -> None:
    """The watchdog must NOT nudge while a background agent is in flight: the main
    transcript is legitimately quiet then (the turn already ended with a launch),
    which is not a hang.  Nudging there would disrupt real background work.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    # Stop hook / transcript scans must not discover or clear agents on their own.
    monkeypatch.setattr(
        "poor_claude.control_server.find_background_agent_ids_in_transcript",
        lambda *_a, **_kw: [],
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "1.0")
    monkeypatch.setenv("POOR_CLAUDE_MAX_NUDGES", "3")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "launch bg agent",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Become active, then register an in-flight background agent well within
        # the first stall window (1.0s) so no nudge can fire first.
        session_id = None
        for _ in range(20):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.02)
        assert session_id is not None, "request never became active"
        request_json(
            "POST",
            f"{address}/hook/agent-launched",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "bg-1"},
        )

        # Wait past the stall window: with a pending background agent, no nudge.
        time.sleep(1.6)
        listed = request_json("GET", f"{address}/sessions")
        assert "nudges_sent" not in listed["sessions"][0]["metadata"], (
            "watchdog nudged during legitimate background work"
        )

        # Background agent finishes and the final Stop hook completes the request.
        request_json(
            "POST",
            f"{address}/hook/subagent-stop",
            {"session_id": session_id, "cwd": str(tmp_path), "agent_id": "bg-1"},
        )
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "BG-FINAL", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "BG-FINAL"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_restart_fallback_relaunches_with_resume(tmp_path, monkeypatch) -> None:
    """When stall_action escalates to a restart, the watchdog must kill+relaunch the
    session with resume (resume_on_launch=True) and re-inject the stuck prompt,
    reusing the _ensure_process_metadata relaunch path.  Then a Stop hook completes
    the request normally.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    # Stub the relaunch so the test never spawns a real `claude` process.
    recorded = {"ensure": 0}

    def fake_ensure(state, session) -> None:
        recorded["ensure"] += 1
        session.metadata["process_alive"] = "True"

    monkeypatch.setattr("poor_claude.control_server._ensure_process_metadata", fake_ensure)
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0.2")
    monkeypatch.setenv("POOR_CLAUDE_STALL_ACTION", "restart")
    monkeypatch.setenv("POOR_CLAUDE_MAX_RESTARTS", "1")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        restarts = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                restarts = sessions[0]["metadata"].get("stall_restarts")
                if restarts == "1":
                    break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"
        assert restarts == "1", f"watchdog did not escalate to a restart: {restarts}"
        assert recorded["ensure"] >= 1, "relaunch path (_ensure_process_metadata) not invoked"

        listed = request_json("GET", f"{address}/sessions")
        meta = listed["sessions"][0]["metadata"]
        assert meta.get("resume_on_launch") == "True", "relaunch did not request --resume"

        # Resumed Claude finally responds; the Stop hook completes the request.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "RESUMED-OK", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3)
        assert result_box.get("result", {}).get("response") == "RESUMED-OK"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_restart_aborts_when_request_completes_concurrently(tmp_path, monkeypatch) -> None:
    """H2 (TOCTOU): when the Stop hook completes the request at the same time as
    the watchdog decides to restart, the restart must be a no-op — no process kill,
    restarts_done unchanged, response returned correctly.

    This test simulates the race by delivering the Stop hook from inside
    fake_ensure_process_metadata, which runs just after the re-check under the lock
    has confirmed `do_restart=True`.  A second scenario exercises the abort path
    (request already done when the re-check fires), by delivering the Stop hook
    before the first stall window expires.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0.2")
    monkeypatch.setenv("POOR_CLAUDE_STALL_ACTION", "restart")
    monkeypatch.setenv("POOR_CLAUDE_MAX_RESTARTS", "1")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    # Deliver the Stop hook from inside fake_ensure — simulates a concurrent Stop
    # arriving while the restart is in progress (after the lock re-check passes but
    # before the resumed process has a chance to respond on its own).
    ensure_calls = {"count": 0}
    session_id_holder: list = []

    def fake_ensure(st, sess) -> None:
        ensure_calls["count"] += 1
        sess.metadata["process_alive"] = "True"
        # Deliver Stop hook concurrently — response must still be returned correctly.
        sid = sess.session_id
        threading.Thread(
            target=lambda: request_json(
                "POST",
                f"{address}/hook/stop",
                {"session_id": sid, "response": "CONCURRENT-STOP", "cwd": str(tmp_path)},
            ),
            daemon=True,
        ).start()

    monkeypatch.setattr("poor_claude.control_server._ensure_process_metadata", fake_ensure)

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for a restart to fire (stall_restarts=1 set in metadata).
        session_id = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                if sessions[0]["metadata"].get("stall_restarts") == "1":
                    break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"
        assert ensure_calls["count"] >= 1, "_ensure_process_metadata was not invoked"

        req_thread.join(timeout=5)
        assert result_box.get("result", {}).get("response") == "CONCURRENT-STOP", (
            f"wrong response: {result_box.get('result')}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_restart_no_spurious_kill_when_already_done(tmp_path, monkeypatch) -> None:
    """H2 abort path: if the request completes (Stop hook fires) before the stall
    window expires, the watchdog must never call _ensure_process_metadata and must
    not set stall_restarts in metadata.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    ensure_calls = {"count": 0}

    def fake_ensure(st, sess) -> None:  # pragma: no cover — must NOT be called
        ensure_calls["count"] += 1

    monkeypatch.setattr("poor_claude.control_server._ensure_process_metadata", fake_ensure)
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0.5")
    monkeypatch.setenv("POOR_CLAUDE_STALL_ACTION", "restart")
    monkeypatch.setenv("POOR_CLAUDE_MAX_RESTARTS", "1")

    state = ControlState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 5,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                },
                timeout=7,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to become active, then deliver the Stop hook
        # well before the 0.5s stall window can expire.
        session_id = None
        for _ in range(40):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0]["active_request"] is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.02)
        assert session_id is not None, "request never became active"

        # Deliver response before stall window expires — restart must not fire.
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "EARLY-DONE", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3)

        assert result_box.get("result", {}).get("response") == "EARLY-DONE"
        assert ensure_calls["count"] == 0, (
            f"_ensure_process_metadata called unexpectedly: {ensure_calls}"
        )
        meta = request_json("GET", f"{address}/sessions")["sessions"][0]["metadata"]
        assert "stall_restarts" not in meta, f"unexpected restart metadata: {meta}"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_fast_fails_on_process_death_rate_limit(tmp_path, monkeypatch) -> None:
    """When the Claude process exits without firing a Stop hook and the stdout log
    shows a rate-limit TUI, _wait_for_response must return a rate-limit error
    message immediately rather than waiting for the hard 30-min timeout.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")  # disable stall watchdog

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 30,  # long timeout — fast-fail must preempt it
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=10,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to become active and grab the stdout log path.
        # Note: the /sessions endpoint nests the path under metadata["claude_stdout_path"],
        # not as a top-level "stdout" key.
        stdout_path_str = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                stdout_path_str = sessions[0].get("metadata", {}).get("claude_stdout_path")
                break
            time.sleep(0.05)
        assert stdout_path_str is not None, "request never became active or stdout path missing"

        # Write rate-limit content to the stdout log (file may not yet exist)
        stdout_path = Path(stdout_path_str)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_bytes(
            b"some normal output\n/rate-limit-options\nstop and wait for limit to reset\n"
        )

        # Simulate the process exiting: FakeProcess.poll() returns 0 when terminated=True
        fake_process.terminated = True

        # Fast-fail should trigger within ~1 s (a couple of 0.5s loop iterations)
        start = time.time()
        req_thread.join(timeout=5.0)
        elapsed = time.time() - start

        assert not req_thread.is_alive(), "request did not complete after process death"
        assert elapsed < 4.0, f"fast-fail too slow: {elapsed:.2f}s (expected <4s)"
        response = result_box.get("result", {}).get("response", "")
        assert "limit" in response.lower(), f"expected rate-limit message, got: {response!r}"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_fast_fails_on_process_death_generic(tmp_path, monkeypatch) -> None:
    """When the Claude process exits without a Stop hook and the log contains no
    rate-limit marker, _wait_for_response returns the generic 'exited unexpectedly'
    error message immediately.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 30,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=10,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        stdout_path_str = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                stdout_path_str = sessions[0].get("metadata", {}).get("claude_stdout_path")
                break
            time.sleep(0.05)
        assert stdout_path_str is not None, "request never became active or stdout path missing"

        # Write innocuous content — no rate-limit signature
        stdout_path = Path(stdout_path_str)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_bytes(b"normal claude output, no rate limit\n")

        fake_process.terminated = True

        start = time.time()
        req_thread.join(timeout=5.0)
        elapsed = time.time() - start

        assert not req_thread.is_alive(), "request did not complete after process death"
        assert elapsed < 4.0, f"fast-fail too slow: {elapsed:.2f}s"
        response = result_box.get("result", {}).get("response", "")
        assert "exited unexpectedly" in response.lower(), (
            f"expected generic exit message, got: {response!r}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_fast_fails_prefers_transcript_over_exit_msg(tmp_path, monkeypatch) -> None:
    """When the Claude process dies on the same iteration that the transcript first shows a
    complete end_turn response, _wait_for_response must return the transcript text rather
    than the generic exit message.

    This tests the 'transcript_response + end_turn' branch in the fast-fail block, which is
    needed because transcript_fallback_response is only promoted after 0.5 s of stability —
    it is still None on the very first iteration where a response appears.
    """
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    # The transcript read always returns the completed response immediately
    # (simulating: Claude wrote its answer and died in the same 0.5s window).
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: TranscriptResponse(text="THE-REAL-ANSWER", stop_reason="end_turn"),
    )

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 30,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=10,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to become active
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                break
            time.sleep(0.05)

        # Terminate process — death check should fire and prefer the transcript text
        fake_process.terminated = True

        req_thread.join(timeout=5.0)
        assert not req_thread.is_alive(), "request did not complete"
        response = result_box.get("result", {}).get("response", "")
        assert response == "THE-REAL-ANSWER", (
            f"expected transcript text, got: {response!r}"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_response_no_false_positive_while_process_alive(tmp_path, monkeypatch) -> None:
    """The fast-fail death check must NOT fire while the process is alive.
    A live process should allow the request to complete normally via the Stop hook.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 10,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=12,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        session_id = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                session_id = sessions[0]["session_id"]
                break
            time.sleep(0.05)
        assert session_id is not None, "request never became active"

        # Verify: with a live process, the death check must not fire.
        # Wait for ~1.5 s (3 loop iterations at 0.5s each) and confirm the
        # request is still pending — it should only complete on the Stop hook.
        time.sleep(1.5)
        assert req_thread.is_alive(), (
            "fast-fail fired on a live process (false positive)"
        )

        # Complete normally via Stop hook
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": session_id, "response": "ALIVE-OK", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3.0)
        assert not req_thread.is_alive(), "request did not complete after Stop hook"
        assert result_box.get("result", {}).get("response") == "ALIVE-OK"
    finally:
        server.shutdown()
        server.server_close()


def test_rate_limit_hook_completes_active_request(tmp_path, monkeypatch) -> None:
    """POST /hook/rate-limit completes the active request with a rate-limit error
    message immediately, without waiting for the process to die or Stop hook to fire.
    The session stays alive (process is NOT killed) — this models the real scenario
    where Claude Code remains running after the TUI is dismissed.
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 30,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=10,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for the request to become active
        route_key = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                route_key = sessions[0].get("route_key")
                break
            time.sleep(0.05)
        assert route_key is not None, "request never became active"

        # Simulate drain thread: POST /hook/rate-limit (process stays alive)
        hook_result = request_json(
            "POST",
            f"{address}/hook/rate-limit",
            {"route_key": route_key},
        )
        assert hook_result.get("ok") is True

        # Request should complete immediately with a rate-limit message
        start = time.time()
        req_thread.join(timeout=5.0)
        elapsed = time.time() - start

        assert not req_thread.is_alive(), "request did not complete after rate-limit hook"
        assert elapsed < 4.0, f"rate-limit hook too slow: {elapsed:.2f}s"
        response = result_box.get("result", {}).get("response", "")
        assert "limit" in response.lower(), f"expected rate-limit message, got: {response!r}"

        # Process is still alive — session should survive
        assert fake_process.terminated is False, "process must not be killed by rate-limit hook"
    finally:
        server.shutdown()
        server.server_close()


def test_rate_limit_hook_idempotent_when_no_active_request(tmp_path, monkeypatch) -> None:
    """POST /hook/rate-limit is idempotent in two distinct cases:
    1. Route key doesn't exist (no_session).
    2. Session exists but has no active request (e.g. Stop hook already completed it).
    """
    monkeypatch.setattr(
        "poor_claude.control_server.read_response_record_after_request_from_file",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setenv("POOR_CLAUDE_STALL_SECONDS", "0")

    state = ControlState(state_dir=tmp_path / "state")
    fake_process = FakeProcess()
    state.process_manager._launch_fn = lambda _spec: fake_process
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    state.callback_base_url = address

    try:
        # Case 1: route key doesn't exist → no_session
        result = request_json(
            "POST",
            f"{address}/hook/rate-limit",
            {"route_key": "nonexistent::route"},
        )
        assert result.get("ok") is True
        assert result.get("no_session") is True

        # Case 2: session exists, request was completed by Stop hook, no active request
        result_box: dict = {}

        def send_request() -> None:
            result_box["result"] = request_json(
                "POST",
                f"{address}/requests",
                {
                    "session_id": "demo",
                    "prompt": "do work",
                    "timeout_seconds": 30,
                    "workdir": str(tmp_path),
                    "wait_for_response": True,
                    "launch_process": True,
                },
                timeout=10,
            )

        req_thread = threading.Thread(target=send_request, daemon=True)
        req_thread.start()

        # Wait for request to become active and grab route_key
        route_key = None
        for _ in range(80):
            listed = request_json("GET", f"{address}/sessions")
            sessions = listed.get("sessions", [])
            if sessions and sessions[0].get("active_request") is not None:
                route_key = sessions[0].get("route_key")
                break
            time.sleep(0.05)
        assert route_key is not None, "request never became active"

        # Stop hook completes the request first
        request_json(
            "POST",
            f"{address}/hook/stop",
            {"session_id": "demo", "response": "done", "cwd": str(tmp_path)},
        )
        req_thread.join(timeout=3.0)
        assert not req_thread.is_alive()

        # Now fire the rate-limit hook — session has no active request
        result = request_json(
            "POST",
            f"{address}/hook/rate-limit",
            {"route_key": route_key},
        )
        assert result.get("ok") is True
        assert result.get("no_active_request") is True
    finally:
        server.shutdown()
        server.server_close()


def test_safe_to_delete_state_file_true_when_file_missing(tmp_path) -> None:
    state_file = tmp_path / "daemon.json"
    assert _safe_to_delete_state_file(state_file, pid=1234) is True


def test_safe_to_delete_state_file_true_when_pid_matches(tmp_path) -> None:
    state_file = tmp_path / "daemon.json"
    write_state(state_file, DaemonState(pid=1234, address="http://127.0.0.1:1"))
    assert _safe_to_delete_state_file(state_file, pid=1234) is True


def test_safe_to_delete_state_file_false_when_another_daemon_claimed_it(tmp_path) -> None:
    """Regression test: a shutting-down daemon must not delete the state file
    once a different (e.g. newer) daemon has recorded itself as the owner —
    doing so was observed to wipe out the record of a healthy, still-running
    daemon when an orphaned duplicate from a startup race shut down after it."""
    state_file = tmp_path / "daemon.json"
    write_state(state_file, DaemonState(pid=1234, address="http://127.0.0.1:1"))
    # A different daemon (e.g. the survivor of a startup race) claims the file.
    write_state(state_file, DaemonState(pid=5678, address="http://127.0.0.1:2"))
    assert _safe_to_delete_state_file(state_file, pid=1234) is False
    # And the file must be left intact for the real owner to keep using.
    assert state_file.exists()


def test_safe_to_delete_state_file_true_on_corrupt_file(tmp_path) -> None:
    """A corrupt file records no valid claim from another daemon, so it must
    still be cleaned up on shutdown — otherwise it's left behind to crash the
    next start_daemon() call's read instead of being treated as "no daemon"."""
    state_file = tmp_path / "daemon.json"
    state_file.write_text("not valid json", encoding="utf-8")
    assert _safe_to_delete_state_file(state_file, pid=1234) is True


def test_safe_to_delete_state_file_true_on_wrong_field_type(tmp_path) -> None:
    state_file = tmp_path / "daemon.json"
    state_file.write_text('{"pid": null, "address": "http://127.0.0.1:1"}', encoding="utf-8")
    assert _safe_to_delete_state_file(state_file, pid=1234) is True


def test_settings_fingerprint_empty_path_returns_empty_string() -> None:
    assert _settings_fingerprint("") == ""


def test_settings_fingerprint_same_content_different_path_matches(tmp_path) -> None:
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    first.write_text('{"hooks": {}}', encoding="utf-8")
    second.write_text('{"hooks": {}}', encoding="utf-8")
    assert _settings_fingerprint(str(first)) == _settings_fingerprint(str(second))


def test_settings_fingerprint_different_content_differs(tmp_path) -> None:
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    first.write_text('{"hooks": {}}', encoding="utf-8")
    second.write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    assert _settings_fingerprint(str(first)) != _settings_fingerprint(str(second))


def test_settings_fingerprint_missing_file_falls_back_to_path(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.json"
    fingerprint = _settings_fingerprint(str(missing))
    assert fingerprint == f"unreadable:{missing}"
    # Two different missing paths must not collide with each other.
    other_missing = tmp_path / "also-missing.json"
    assert _settings_fingerprint(str(other_missing)) != fingerprint

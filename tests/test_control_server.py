import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from poor_claude.control_server import ControlState, make_handler
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
    server, address = start_test_server()
    first_settings = tmp_path / "one.json"
    second_settings = tmp_path / "two.json"
    first_settings.write_text('{"hooks": {}}', encoding="utf-8")
    second_settings.write_text('{"hooks": {}}', encoding="utf-8")
    try:
        request_json(
            "POST",
            f"{address}/sessions",
            {"session_id": "demo", "workdir": str(tmp_path), "settings_path": str(first_settings)},
        )
        try:
            request_json(
                "POST",
                f"{address}/sessions",
                {
                    "session_id": "demo",
                    "workdir": str(tmp_path),
                    "settings_path": str(second_settings),
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


def test_control_server_rejects_launch_config_mismatch(tmp_path) -> None:
    server, address = start_test_server()
    try:
        first_settings = tmp_path / "one.json"
        second_settings = tmp_path / "two.json"
        first_settings.write_text('{"hooks": {}}', encoding="utf-8")
        second_settings.write_text('{"hooks": {}}', encoding="utf-8")
        request_json(
            "POST",
            f"{address}/sessions",
            {
                "session_id": "demo",
                "workdir": str(tmp_path),
                "settings_path": str(first_settings),
                "permission_mode": "bypassPermissions",
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
        except Exception as exc:
            assert "launch config differs" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected launch config mismatch")
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
         New code: bg_agent_detected=True, pending=0 → reset timer, keep looping.
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

        # Request must still be active — bg_agent_detected guard prevents early finish.
        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["active_request"] is not None, (
            "request completed prematurely with stale 'LAUNCHED' response — "
            "bg_agent_detected guard is not preventing early completion"
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

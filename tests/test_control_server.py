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


def test_control_server_rejects_stop_hook_without_request_id(tmp_path) -> None:
    server, address = start_test_server()
    try:
        request_json(
            "POST",
            f"{address}/requests",
            {"session_id": "demo", "prompt": "hello", "workdir": str(tmp_path)},
        )
        try:
            request_json(
                "POST",
                f"{address}/hook/stop",
                {"session_id": "demo", "response": "late", "cwd": str(tmp_path)},
            )
        except Exception as exc:
            assert "missing request_id" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing request_id rejection")
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

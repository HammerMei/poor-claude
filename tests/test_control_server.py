import threading
import time
from http.server import ThreadingHTTPServer

from poor_claude.control_server import ControlState, make_handler
from poor_claude.http_client import request_json


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
                "dangerously_skip_permissions": True,
            },
        )
        assert created["session_id"] == "demo"

        listed = request_json("GET", f"{address}/sessions")
        assert listed["sessions"][0]["session_id"] == "demo"
        assert listed["sessions"][0]["metadata"]["settings_path"] == str(settings_path)
        assert listed["sessions"][0]["metadata"]["dangerously_skip_permissions"] == "True"
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
                "dangerously_skip_permissions": True,
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
                    "dangerously_skip_permissions": True,
                },
            )
        except Exception as exc:
            assert "launch config differs" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected launch config mismatch")
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

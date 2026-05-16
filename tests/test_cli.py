from io import StringIO
from types import SimpleNamespace

import pytest

from poor_claude import cli
from poor_claude.http_client import HttpClientError


SESSION_ID = "11111111-1111-4111-8111-111111111111"


class FakeStdin(StringIO):
    def __init__(self, value: str = "", *, is_tty: bool = True) -> None:
        super().__init__(value)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_cli_dry_run_prints_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    exit_code = cli.main(["--dry-run", "--session-id", SESSION_ID, "-p", "hello"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f'"session_id": "{SESSION_ID}"' in output
    assert '"prompt": "hello"' in output
    assert '"prompt_source": "print"' in output
    assert '"print_mode": true' in output
    assert '"auto_accept_startup_prompts": true' in output


def test_cli_rejects_prompt_conflict(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "hello", "world"])
    assert exc.value.code == 2


def test_cli_resume_is_reported_in_dry_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    exit_code = cli.main(["--dry-run", "-p", "--resume", SESSION_ID])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f'"resume": "{SESSION_ID}"' in output
    assert '"prompt": "hello"' in output


def test_cli_print_flag_accepts_prompt_value(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    exit_code = cli.main(["--dry-run", "-p", "hello"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"prompt": "hello"' in output
    assert '"prompt_source": "print"' in output


def test_cli_auto_accept_startup_prompts_can_be_disabled(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    exit_code = cli.main(["--dry-run", "--no-auto-accept-startup-prompts", "-p", "hello"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"auto_accept_startup_prompts": false' in output


def test_cli_old_auto_accept_workspace_trust_alias_still_works(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    exit_code = cli.main(["--dry-run", "--no-auto-accept-startup-prompts", "--auto-accept-workspace-trust", "-p", "hello"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"auto_accept_startup_prompts": true' in output


def test_cli_accepts_short_resume_flag(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    exit_code = cli.main(["--dry-run", "-p", "-r", SESSION_ID])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f'"resume": "{SESSION_ID}"' in output


def test_cli_rejects_bare_resume_picker(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "-p", "-r"])
    assert exc.value.code == 2


def test_cli_rejects_ambiguous_short_resume_prompt(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "-p", "-r", "hello"])
    assert exc.value.code == 2


def test_cli_canonicalizes_uuid(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    exit_code = cli.main(["--dry-run", "-p", "--session-id", SESSION_ID.replace("-", "")])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f'"session_id": "{SESSION_ID}"' in output


def test_cli_rejects_session_id_and_resume(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "-p", "--session-id", "a", "--resume", "b"])
    assert exc.value.code == 2


def test_cli_accepts_named_session_id(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    exit_code = cli.main(["--dry-run", "-p", "--session-id", "not-a-uuid"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"session_id": "not-a-uuid"' in output


def test_format_diagnostics_includes_paths_and_summaries() -> None:
    text = cli._format_diagnostics(
        {
            "session_id": SESSION_ID,
            "paths": {"stdout": "/tmp/out.log"},
            "summaries": {"stdout": "Error: boom"},
        }
    )
    assert "Diagnostics:" in text
    assert "/tmp/out.log" in text
    assert "Error: boom" in text


def test_format_sessions_prints_readable_table() -> None:
    text = cli._format_sessions(
        [
            {
                "session_id": SESSION_ID,
                "active_request": None,
                "keep_alive": False,
                "ttl_seconds": 900,
                "workdir": "/tmp/project",
                "metadata": {"process_alive": "False", "resume_on_launch": "True"},
            }
        ]
    )
    assert "SESSION" in text
    assert "stopped" in text
    assert "yes" in text
    assert "/tmp/project" in text


def test_format_sessions_handles_empty_list() -> None:
    assert cli._format_sessions([]) == "No active sessions."


def test_cli_shutdown_returns_zero_when_daemon_missing(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "discover_state", lambda _path: None)
    assert cli.main(["--shutdown"]) == 0


def test_cli_shutdown_posts_to_daemon(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "discover_state", lambda _path: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(cli, "request_json", lambda method, url, payload=None, **kwargs: calls.append((method, url, payload)))
    assert cli.main(["--shutdown"]) == 0
    assert calls == [("POST", "http://daemon/shutdown", {})]


def test_cli_sessions_prints_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(cli, "request_json", lambda method, url, **kwargs: {"sessions": [{"session_id": "demo"}]})
    assert cli.main(["--sessions", "--json"]) == 0
    assert '"session_id": "demo"' in capsys.readouterr().out


def test_cli_prune_prints_count(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(cli, "request_json", lambda method, url, payload=None, **kwargs: {"removed_routes": ["a", "b"]})
    assert cli.main(["--prune-sessions"]) == 0
    assert "Pruned 2 session(s)." in capsys.readouterr().out


def test_cli_stop_session_requires_id(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--stop-session"])
    assert exc.value.code == 2


def test_cli_stop_session_calls_delete(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda method, url, payload=None, headers=None, **kwargs: calls.append((method, url, headers)) or {"ok": True},
    )
    assert cli.main(["--stop-session", "--session-id", SESSION_ID, "--workdir", "/tmp/demo"]) == 0
    assert calls == [("DELETE", f"http://daemon/sessions/{SESSION_ID}", {"X-Poor-Claude-Workdir": "/tmp/demo"})]
    assert '"ok": true' in capsys.readouterr().out


def test_cli_start_session_posts_expected_payload(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda method, url, payload=None, **kwargs: calls.append((method, url, payload)) or {"session_id": "demo"},
    )
    assert cli.main(["--start-session", "--session-id", "demo", "--no-auto-accept-startup-prompts"]) == 0
    payload = calls[0][2]
    assert calls[0][0:2] == ("POST", "http://daemon/sessions")
    assert payload["session_id"] == "demo"
    assert payload["auto_accept_workspace_trust"] is False
    assert '"session_id": "demo"' in capsys.readouterr().out


def test_cli_request_outputs_stream_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda method, url, payload=None, timeout=None, **kwargs: {"session_id": "demo", "response": "hello"},
    )
    assert cli.main(["-p", "prompt", "--output-format", "stream-json"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert '"type": "assistant"' in lines[0]
    assert '"type": "result"' in lines[1]


def test_cli_request_outputs_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda method, url, payload=None, timeout=None, **kwargs: {"session_id": "demo", "response": "hello"},
    )
    assert cli.main(["-p", "prompt", "--output-format", "json"]) == 0
    assert capsys.readouterr().out.strip() == '{"session_id": "demo", "result": "hello"}'


def test_cli_request_debug_writes_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))
    monkeypatch.setattr(
        cli,
        "request_json",
        lambda method, url, payload=None, timeout=None, **kwargs: {"session_id": "demo", "response": "hello"},
    )
    assert cli.main(["-p", "prompt", "--debug"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "hello"
    assert '"response": "hello"' in captured.err


def test_cli_http_error_prints_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    monkeypatch.setattr(cli, "default_state_path", lambda: "/tmp/daemon.json")
    monkeypatch.setattr(cli, "start_daemon", lambda **kwargs: SimpleNamespace(address="http://daemon"))

    def fail_request(*_args, **_kwargs):
        raise HttpClientError("boom", payload={"diagnostics": {"session_id": "demo", "paths": {"stdout": "/tmp/out"}}})

    monkeypatch.setattr(cli, "request_json", fail_request)
    assert cli.main(["-p", "prompt"]) == 1
    stderr = capsys.readouterr().err
    assert "claude-no-p failed: boom" in stderr
    assert "/tmp/out" in stderr

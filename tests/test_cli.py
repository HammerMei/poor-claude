from io import StringIO

import pytest

from poor_claude import cli


class FakeStdin(StringIO):
    def __init__(self, value: str = "", *, is_tty: bool = True) -> None:
        super().__init__(value)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_cli_dry_run_prints_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    exit_code = cli.main(["--dry-run", "--session-id", "demo", "-p", "hello"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"session_id": "demo"' in output
    assert '"prompt": "hello"' in output
    assert '"print_mode": true' in output


def test_cli_rejects_prompt_conflict(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(is_tty=True))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "hello", "world"])
    assert exc.value.code == 2


def test_cli_resume_is_reported_in_dry_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    exit_code = cli.main(["--dry-run", "-p", "--resume", "demo"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"resume": "demo"' in output
    assert '"prompt": "hello"' in output


def test_cli_rejects_session_id_and_resume(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("hello", is_tty=False))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--dry-run", "-p", "--session-id", "a", "--resume", "b"])
    assert exc.value.code == 2

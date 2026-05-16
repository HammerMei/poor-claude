import json
import sys
from pathlib import Path

import poor_claude.identity_validation as identity_validation
from poor_claude.identity_validation import build_dump_settings, validate_session_identity


def test_build_dump_settings_uses_local_output_file() -> None:
    settings = build_dump_settings(output_path=Path("/tmp/hook.json"))
    hook = settings["hooks"]["Stop"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert sys.executable in hook["command"]
    assert "-c" in hook["command"]
    assert "/tmp/hook.json" in hook["command"]


def test_validate_session_identity_reads_matching_payload(tmp_path, monkeypatch) -> None:
    session_id = "demo"

    def fake_run(command, **kwargs):
        artifact_dir = kwargs["cwd"] / ".poor-claude-validation"
        payload_path = artifact_dir / f"stop-hook-{session_id}.json"
        payload_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
        debug_path = Path(command[4])
        debug_path.write_text("debug", encoding="utf-8")

    monkeypatch.setattr(identity_validation.subprocess, "run", fake_run)
    result = validate_session_identity(session_id=session_id, workdir=tmp_path)
    assert result.matched is True
    assert result.observed_session_id == session_id
    assert result.hook_payload_path.exists()
    assert result.settings_path.exists()


def test_validate_session_identity_handles_missing_payload(tmp_path, monkeypatch) -> None:
    now = {"value": 0.0}

    def fake_time():
        now["value"] += 11.0
        return now["value"]

    monkeypatch.setattr(identity_validation.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(identity_validation.time, "time", fake_time)
    monkeypatch.setattr(identity_validation.time, "sleep", lambda _seconds: None)
    result = validate_session_identity(session_id="demo", workdir=tmp_path)
    assert result.matched is False
    assert result.observed_session_id is None


def test_identity_validation_main_returns_success(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        identity_validation,
        "validate_session_identity",
        lambda **kwargs: identity_validation.IdentityValidationResult(
            requested_session_id="demo",
            observed_session_id="demo",
            matched=True,
            hook_payload_path=tmp_path / "hook.json",
            settings_path=tmp_path / "settings.json",
        ),
    )
    assert identity_validation.main(["--session-id", "demo", "--workdir", str(tmp_path)]) == 0
    assert '"matched": true' in capsys.readouterr().out

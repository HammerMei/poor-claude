import json
from pathlib import Path

import poor_claude.mcp_validation as mcp_validation


def test_validate_mcp_connection_reports_connected(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        artifact_dir = kwargs["cwd"] / ".poor-claude-validation"
        log_path = artifact_dir / "mcp-stdio.log"
        log_path.write_text(
            "\n".join(
                [
                    json.dumps({"event": "server_start"}),
                    json.dumps({"message": {"method": "initialize"}}),
                ]
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(mcp_validation.subprocess, "run", fake_run)
    result = mcp_validation.validate_mcp_connection(workdir=tmp_path)
    assert result.connected is True
    assert result.log_path.exists()
    assert result.config_path.exists()
    assert len(result.events) == 2


def test_validate_mcp_connection_handles_missing_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_validation.subprocess, "run", lambda *args, **kwargs: None)
    result = mcp_validation.validate_mcp_connection(workdir=tmp_path)
    assert result.connected is False
    assert result.events == []


def test_mcp_validation_main_returns_failure(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        mcp_validation,
        "validate_mcp_connection",
        lambda **kwargs: mcp_validation.McpValidationResult(
            connected=False,
            log_path=tmp_path / "mcp.log",
            config_path=tmp_path / "mcp.json",
            events=[],
        ),
    )
    assert mcp_validation.main() == 1
    assert '"connected": false' in capsys.readouterr().out

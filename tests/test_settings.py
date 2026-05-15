import json
import sys

from poor_claude.settings import (
    build_settings,
    merge_settings,
    read_settings,
    stop_hook_command,
    write_merged_settings,
    write_settings,
)


def test_stop_hook_command_uses_local_module() -> None:
    command = stop_hook_command("http://127.0.0.1:1234/hook/stop")
    assert sys.executable in command
    assert "-m poor_claude.hooks.stop_hook" in command
    assert "PYTHONPATH=" in command
    assert "http://127.0.0.1:1234/hook/stop" in command


def test_build_settings_contains_stop_hook_only() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop")
    assert list(settings["hooks"].keys()) == ["Stop"]
    hook = settings["hooks"]["Stop"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "poor_claude.hooks.stop_hook" in hook["command"]


def test_write_settings_writes_local_file(tmp_path) -> None:
    generated = write_settings(
        directory=tmp_path,
        callback_url="http://127.0.0.1:1234/hook/stop",
    )
    assert generated.path == tmp_path / "claude-settings.local.json"
    assert json.loads(generated.path.read_text(encoding="utf-8")) == generated.data


def test_merge_settings_preserves_existing_hooks_and_adds_stop_hook() -> None:
    base = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "broker"}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "existing-stop"}]}
            ],
        },
        "permissions": {"allow": ["Bash(ls *)"]},
    }
    merged = merge_settings(base, build_settings("http://127.0.0.1:1234/hook/stop"))
    assert merged["permissions"] == base["permissions"]
    assert merged["hooks"]["PreToolUse"] == base["hooks"]["PreToolUse"]
    assert len(merged["hooks"]["Stop"]) == 2
    assert "poor_claude.hooks.stop_hook" in merged["hooks"]["Stop"][1]["hooks"][0]["command"]


def test_read_settings_accepts_file_or_json(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"hooks": {}}', encoding="utf-8")
    assert read_settings(str(path)) == {"hooks": {}}
    assert read_settings('{"permissions": {}}') == {"permissions": {}}


def test_write_merged_settings_writes_unique_local_file(tmp_path) -> None:
    base_path = tmp_path / "base.json"
    base_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "broker"}]}]}}),
        encoding="utf-8",
    )
    generated = write_merged_settings(
        directory=tmp_path,
        callback_url="http://127.0.0.1:1234/hook/stop",
        base_settings_path_or_json=str(base_path),
    )
    assert generated.path.name.startswith("claude-settings.merged.")
    assert "PreToolUse" in generated.data["hooks"]
    assert "Stop" in generated.data["hooks"]

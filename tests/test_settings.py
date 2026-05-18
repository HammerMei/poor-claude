import json
import sys
from pathlib import Path

from poor_claude.settings import (
    build_settings,
    cleanup_project_local_settings,
    ensure_skip_dangerous_mode_prompt,
    merge_settings,
    read_settings,
    strip_poor_claude_managed_settings,
    stop_hook_command,
    subagent_stop_hook_command,
    write_project_local_settings,
    write_merged_settings,
    write_settings,
)


def test_stop_hook_command_uses_local_module() -> None:
    command = stop_hook_command("http://127.0.0.1:1234/hook/stop")
    assert sys.executable in command
    assert "-m poor_claude.hooks.stop_hook" in command
    assert "--poor-claude-managed" in command
    assert "PYTHONPATH=" in command
    assert "http://127.0.0.1:1234/hook/stop" in command


def test_build_settings_contains_stop_hook_only() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop")
    assert "permissions" not in settings
    assert list(settings["hooks"].keys()) == ["PreToolUse", "Stop"]
    hook = settings["hooks"]["Stop"][0]["hooks"][0]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == ".*"
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"
    assert "poor_claude.hooks.pretool_hook" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--poor-claude-managed" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook["type"] == "command"
    assert "poor_claude.hooks.stop_hook" in hook["command"]


def test_build_settings_omits_pretool_hook_when_not_requested() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop", include_pretool_hook=False)
    assert "PreToolUse" not in settings["hooks"]
    assert "Stop" in settings["hooks"]
    assert "poor_claude.hooks.stop_hook" in settings["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_build_settings_embeds_extra_pretool_allow_rules_in_hook_command() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        extra_pretool_allow_rules=["Bash(ls *)", "Read"],
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--allow" in cmd
    assert "Bash(ls *)" in cmd
    assert "Read" in cmd


def test_build_settings_embeds_extra_pretool_disallow_rules_in_hook_command() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        extra_pretool_disallow_rules=["Bash(rm *)"],
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--disallow" in cmd
    assert "Bash(rm *)" in cmd


def test_build_settings_no_allow_flags_when_extra_rules_empty() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop", extra_pretool_allow_rules=None)
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--allow" not in cmd
    assert "--disallow" not in cmd


def test_build_settings_embeds_policy_file_in_hook_command() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        policy_file=Path("/tmp/route-x/tools-policy.json"),
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--policy-file" in cmd
    assert "tools-policy.json" in cmd


def test_build_settings_no_policy_file_flag_when_not_set() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop")
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--policy-file" not in cmd


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
    assert merged["permissions"]["allow"] == base["permissions"]["allow"]
    assert "deny" not in merged["permissions"]
    assert merged["hooks"]["PreToolUse"][0] == base["hooks"]["PreToolUse"][0]
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["type"] == "command"
    assert "poor_claude.hooks.pretool_hook" in merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"]
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
    assert generated.data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "broker"
    assert generated.data["hooks"]["PreToolUse"][1]["hooks"][0]["type"] == "command"
    assert "PreToolUse" in generated.data["hooks"]
    assert "Stop" in generated.data["hooks"]


def test_merge_settings_appends_deny_rule_without_overwriting_allow() -> None:
    base = {
        "permissions": {
            "allow": ["Bash(ls *)"],
            "deny": ["Edit(secret/**)"],
        }
    }
    merged = merge_settings(base, build_settings("http://127.0.0.1:1234/hook/stop"))
    assert merged["permissions"]["allow"] == ["Bash(ls *)"]
    assert merged["permissions"]["deny"] == ["Edit(secret/**)"]


def test_write_project_local_settings_preserves_existing_local_fields(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    existing_path = claude_dir / "settings.local.json"
    existing_path.write_text(json.dumps({"enabledMcpjsonServers": ["poor-claude"]}), encoding="utf-8")
    generated = write_project_local_settings(
        project_dir=tmp_path,
        state_dir=tmp_path / ".poor-claude-state",
        callback_url="http://127.0.0.1:1234/hook/stop",
    )
    assert generated.path == existing_path
    assert generated.data["enabledMcpjsonServers"] == ["poor-claude"]
    assert "poor_claude.hooks.pretool_hook" in generated.data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_write_project_local_settings_replaces_previous_poor_claude_hooks(tmp_path) -> None:
    first = write_project_local_settings(
        project_dir=tmp_path,
        state_dir=tmp_path / ".poor-claude-state",
        callback_url="http://127.0.0.1:1111/hook/stop",
    )
    second = write_project_local_settings(
        project_dir=tmp_path,
        state_dir=tmp_path / ".poor-claude-state",
        callback_url="http://127.0.0.1:2222/hook/stop",
    )
    assert first.path == second.path
    hooks = second.data["hooks"]
    assert len(hooks["PreToolUse"]) == 1
    assert len(hooks["Stop"]) == 1
    assert "http://127.0.0.1:2222/hook/stop" in hooks["Stop"][0]["hooks"][0]["command"]


def test_cleanup_project_local_settings_removes_only_poor_claude_entries(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    generated = write_project_local_settings(
        project_dir=tmp_path,
        state_dir=tmp_path / ".poor-claude-state",
        callback_url="http://127.0.0.1:1234/hook/stop",
    )
    merged = generated.data
    merged["enabledMcpjsonServers"] = ["poor-claude"]
    settings_path.write_text(json.dumps(merged), encoding="utf-8")
    cleanup_project_local_settings(tmp_path)
    cleaned = json.loads(settings_path.read_text(encoding="utf-8"))
    assert cleaned == {"enabledMcpjsonServers": ["poor-claude"]}


def test_strip_poor_claude_managed_settings_removes_managed_rules() -> None:
    stripped = strip_poor_claude_managed_settings(
        build_settings("http://127.0.0.1:1234/hook/stop", permission_log_path=Path("/tmp/permission.log"))
    )
    assert stripped == {}


def test_ensure_skip_dangerous_mode_prompt_creates_settings_file_when_missing(tmp_path) -> None:
    path = tmp_path / "settings.json"
    ensure_skip_dangerous_mode_prompt(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["skipDangerousModePermissionPrompt"] is True


def test_ensure_skip_dangerous_mode_prompt_adds_key_to_existing_settings(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"someOtherKey": "preserved"}), encoding="utf-8")
    ensure_skip_dangerous_mode_prompt(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["skipDangerousModePermissionPrompt"] is True
    assert data["someOtherKey"] == "preserved"


def test_ensure_skip_dangerous_mode_prompt_is_noop_when_already_set(tmp_path) -> None:
    path = tmp_path / "settings.json"
    original = {"skipDangerousModePermissionPrompt": True, "otherKey": "value"}
    path.write_text(json.dumps(original), encoding="utf-8")
    mtime_before = path.stat().st_mtime_ns
    ensure_skip_dangerous_mode_prompt(path)
    assert path.stat().st_mtime_ns == mtime_before, "file should not be rewritten when key already set"
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_subagent_stop_hook_command_uses_local_module() -> None:
    command = subagent_stop_hook_command("http://127.0.0.1:1234/hook/subagent-stop")
    assert sys.executable in command
    assert "-m poor_claude.hooks.subagent_stop_hook" in command
    assert "--poor-claude-managed" in command
    assert "PYTHONPATH=" in command
    assert "http://127.0.0.1:1234/hook/subagent-stop" in command


def test_build_settings_without_agent_started_url_has_no_subagent_stop_hook() -> None:
    settings = build_settings("http://127.0.0.1:1234/hook/stop")
    assert "SubagentStop" not in settings["hooks"]
    pretool_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--agent-started-url" not in pretool_cmd


def test_build_settings_with_agent_started_url_adds_subagent_stop_hook() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        agent_started_url="http://127.0.0.1:1234/hook/agent-started",
    )
    assert "SubagentStop" in settings["hooks"]
    subagent_hook = settings["hooks"]["SubagentStop"][0]["hooks"][0]
    assert subagent_hook["type"] == "command"
    assert "poor_claude.hooks.subagent_stop_hook" in subagent_hook["command"]
    assert "http://127.0.0.1:1234/hook/subagent-stop" in subagent_hook["command"]


def test_build_settings_with_agent_started_url_adds_flag_to_pretool_hook() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        agent_started_url="http://127.0.0.1:1234/hook/agent-started",
    )
    pretool_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--agent-started-url" in pretool_cmd
    assert "http://127.0.0.1:1234/hook/agent-started" in pretool_cmd


def test_build_settings_with_agent_started_url_keeps_subagent_stop_hook_when_pretool_excluded() -> None:
    # SubagentStop must still be registered even when the pretool hook is
    # disabled (bypassPermissions mode): background agents can still run, and
    # the control server still needs the decrement signal.
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        include_pretool_hook=False,
        agent_started_url="http://127.0.0.1:1234/hook/agent-started",
    )
    assert "SubagentStop" in settings["hooks"]
    assert "PreToolUse" not in settings["hooks"]


def test_build_settings_accepts_explicit_subagent_stop_url() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        agent_started_url="http://127.0.0.1:1234/hook/agent-started",
        subagent_stop_url="http://127.0.0.1:1234/hook/subagent-stop",
    )
    subagent_hook_cmd = settings["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
    assert "http://127.0.0.1:1234/hook/subagent-stop" in subagent_hook_cmd


def test_build_settings_raises_when_agent_started_url_cannot_be_derived() -> None:
    import pytest
    with pytest.raises(ValueError, match="subagent_stop_url"):
        build_settings(
            "http://127.0.0.1:1234/hook/stop",
            agent_started_url="http://127.0.0.1:1234/hook/notify",  # missing /hook/agent-started
        )


def test_strip_poor_claude_managed_settings_removes_subagent_stop_hook() -> None:
    settings = build_settings(
        "http://127.0.0.1:1234/hook/stop",
        agent_started_url="http://127.0.0.1:1234/hook/agent-started",
    )
    stripped = strip_poor_claude_managed_settings(settings)
    assert stripped == {}

from pathlib import Path

import json
import subprocess

import poor_claude.launcher as launcher
from poor_claude.launcher import ClaudeLaunchSpec, _startup_acceptance_keys, build_claude_command, prepare_launch_spec
from poor_claude.session import SessionRegistry


def test_build_claude_command_uses_local_settings_and_resume_id() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=Path("/tmp/settings.json"),
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            stdout_path=Path("/tmp/out.log"),
            stderr_path=Path("/tmp/err.log"),
        )
    )
    assert command == [
        "claude",
        "--session-id",
        "demo",
        "--settings",
        "/tmp/settings.json",
        "--effort",
        "medium",
        "--dangerously-load-development-channels",
        "server:poor-claude",
        "--mcp-config",
        "/tmp/mcp.json",
    ]


def test_build_claude_command_passes_permission_mode_when_non_default() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=Path("/tmp/settings.json"),
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            permission_mode="bypassPermissions",
        )
    )
    assert "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_claude_command_omits_permission_mode_when_default() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=Path("/tmp/settings.json"),
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--permission-mode" not in command


def test_build_claude_command_can_resume_existing_session() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=Path("/tmp/settings.json"),
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            resume=True,
        )
    )
    assert command[1:3] == ["--resume", "demo"]


def test_build_claude_command_can_skip_settings_flag() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--settings" not in command


def test_build_claude_command_uses_custom_effort() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            effort="high",
        )
    )
    effort_idx = command.index("--effort")
    assert command[effort_idx + 1] == "high"


def test_build_claude_command_appends_model_when_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            model="claude-opus-4-5",
        )
    )
    assert "--model" in command
    assert command[command.index("--model") + 1] == "claude-opus-4-5"


def test_build_claude_command_omits_model_when_not_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--model" not in command


def test_build_claude_command_appends_append_system_prompt_when_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            append_system_prompt="be concise",
        )
    )
    assert "--append-system-prompt" in command
    assert command[command.index("--append-system-prompt") + 1] == "be concise"


def test_build_claude_command_omits_append_system_prompt_when_not_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--append-system-prompt" not in command


def test_build_claude_command_passes_system_prompt_when_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            system_prompt="You are a helpful assistant.",
        )
    )
    assert "--system-prompt" in command
    assert command[command.index("--system-prompt") + 1] == "You are a helpful assistant."


def test_build_claude_command_omits_system_prompt_when_not_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--system-prompt" not in command


def test_build_claude_command_passes_tools_when_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            tools=["Bash", "Edit"],
        )
    )
    idx = command.index("--tools")
    assert command[idx + 1] == "Bash"
    assert command[idx + 2] == "Edit"


def test_build_claude_command_passes_empty_string_when_tools_is_empty_list() -> None:
    """tools=[] means disable all tools — passes --tools ""."""
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            tools=[],
        )
    )
    idx = command.index("--tools")
    assert command[idx + 1] == ""


def test_build_claude_command_omits_tools_when_not_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--tools" not in command


def test_build_claude_command_passes_add_dirs_when_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
            add_dirs=["/data", "/shared"],
        )
    )
    # Each directory should be preceded by --add-dir
    assert command.count("--add-dir") == 2
    idx = command.index("--add-dir")
    assert command[idx + 1] == "/data"
    assert command[idx + 3] == "/shared"


def test_build_claude_command_omits_add_dir_when_not_set() -> None:
    command = build_claude_command(
        ClaudeLaunchSpec(
            session_id="demo",
            settings_path=None,
            mcp_config_path=Path("/tmp/mcp.json"),
            channel_name="poor-claude",
            workdir=Path("/tmp"),
        )
    )
    assert "--add-dir" not in command


def test_launch_claude_closes_pty_fds_when_popen_fails(monkeypatch) -> None:
    closed = []
    monkeypatch.setattr(launcher.pty, "openpty", lambda: (10, 11))

    def raise_popen(*_args, **_kwargs):
        raise OSError("launch failed")

    monkeypatch.setattr(launcher.subprocess, "Popen", raise_popen)
    monkeypatch.setattr(launcher.os, "close", lambda fd: closed.append(fd))
    try:
        launcher.launch_claude(
            ClaudeLaunchSpec(
                session_id="demo",
                settings_path=Path("/tmp/settings.json"),
                mcp_config_path=Path("/tmp/mcp.json"),
                channel_name="poor-claude",
                workdir=Path("/missing"),
            )
        )
    except OSError as exc:
        assert "launch failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected launch failure")
    assert closed == [10, 11]


def test_launch_claude_non_pty_uses_file_handles(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    spec = ClaudeLaunchSpec(
        session_id="demo",
        settings_path=tmp_path / "settings.json",
        mcp_config_path=tmp_path / "mcp.json",
        channel_name="poor-claude",
        workdir=tmp_path,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        use_pty=False,
    )
    launcher.launch_claude(spec)
    assert captured["command"][0] == "claude"
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdout"].name.endswith("out.log")
    assert captured["kwargs"]["stderr"].name.endswith("err.log")


def test_startup_acceptance_ignores_bypass_permissions_prompt() -> None:
    """bypass-permissions is handled via ensure_skip_dangerous_mode_prompt(), not key sequences."""
    keys, name = _startup_acceptance_keys(
        "WARNING: Claude Code running in Bypass Permissions mode 1. No, exit 2. Yes, I accept Enter to confirm"
    )
    assert keys is None
    assert name is None


def test_startup_acceptance_accepts_mcp_and_dev_channel_defaults() -> None:
    mcp_keys, mcp_name = _startup_acceptance_keys(
        "New MCP server found in .mcp.json: poor-claude 1. Use this and all future MCP servers 2. Use this MCP server 3. Continue without using this MCP server Enter to confirm"
    )
    dev_keys, dev_name = _startup_acceptance_keys(
        "WARNING: Loading development channels 1. I am using this for local development 2. Exit Enter to confirm"
    )
    # App Cursor Keys mode Down to select option-2 ("use once"), then Enter.
    assert mcp_keys == [b"\x1bOB", b"\r"]
    assert mcp_name == "mcp-server"
    # development-channels: option-1 is default, just confirm with Enter.
    assert dev_keys == [b"\r"]
    assert dev_name == "development-channels"


def test_startup_acceptance_does_not_repeat_accepted_prompt() -> None:
    text = "WARNING: Bypass Permissions 1. No, exit 2. Yes, I accept Enter to confirm"
    assert _startup_acceptance_keys(text, {"bypass-permissions"}) == (None, None)


def test_startup_acceptance_ignores_explanatory_mentions() -> None:
    text = "A later assistant message says bypass permissions yes accept, but this is not a prompt."
    assert _startup_acceptance_keys(text) == (None, None)


def test_plain_terminal_text_strips_ansi_sequences() -> None:
    text = launcher._plain_terminal_text("\x1b[31mWARNING:\x1b[0m  Loading   development channels")
    assert text == "warning: loading development channels"


def test_drain_pty_to_log_auto_accepts_dev_channels_prompt(tmp_path, monkeypatch) -> None:
    """development-channels prompt (option-1 default) is accepted with a single Enter."""
    log_path = tmp_path / "pty.log"
    writes = []
    chunks = iter(
        [
            b"WARNING: Loading development channels 1. I am using this for local development 2. Exit Enter to confirm",
            b"",
        ]
    )

    monkeypatch.setattr(launcher.os, "read", lambda fd, size: next(chunks))
    monkeypatch.setattr(launcher.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launcher._drain_pty_to_log(9, log_path, auto_accept_startup_prompts=True)
    assert writes == [(9, b"\r")]
    assert "auto-accept startup prompt: development-channels" in log_path.read_text(encoding="utf-8")


def test_drain_pty_to_log_bypass_permissions_prompt_not_handled_via_keys(tmp_path, monkeypatch) -> None:
    """bypass-permissions is suppressed via settings, not key injection — no writes expected."""
    log_path = tmp_path / "pty.log"
    writes = []
    chunks = iter(
        [
            b"WARNING: Claude Code running in Bypass Permissions mode 1. No, exit 2. Yes, I accept Enter to confirm",
            b"",
        ]
    )

    monkeypatch.setattr(launcher.os, "read", lambda fd, size: next(chunks))
    monkeypatch.setattr(launcher.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: 0.0)
    launcher._drain_pty_to_log(9, log_path, auto_accept_startup_prompts=True)
    assert writes == []


def test_prepare_launch_spec_calls_ensure_skip_prompt_when_auto_accept_enabled(tmp_path, monkeypatch) -> None:
    """When auto_accept_workspace_trust=True, prepare_launch_spec pre-writes the settings key."""
    called = []
    monkeypatch.setattr(launcher, "ensure_skip_dangerous_mode_prompt", lambda: called.append(True))

    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo", ttl_seconds=3600, keep_alive=False, workdir=str(tmp_path)
    )
    session.metadata["auto_accept_workspace_trust"] = "True"
    prepare_launch_spec(session=session, state_dir=tmp_path, callback_base_url="http://127.0.0.1:1234")
    assert called == [True]


def test_prepare_launch_spec_does_not_call_ensure_skip_prompt_when_auto_accept_disabled(tmp_path, monkeypatch) -> None:
    """Without auto_accept_workspace_trust the settings key must not be touched."""
    called = []
    monkeypatch.setattr(launcher, "ensure_skip_dangerous_mode_prompt", lambda: called.append(True))

    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo", ttl_seconds=3600, keep_alive=False, workdir=str(tmp_path)
    )
    prepare_launch_spec(session=session, state_dir=tmp_path, callback_base_url="http://127.0.0.1:1234")
    assert called == []


def test_prepare_launch_spec_writes_policy_file_and_embeds_path_in_hook(tmp_path) -> None:
    """allowed/disallowed_tools are written to a policy file; hook references it via --policy-file."""
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=3600,
        keep_alive=False,
        workdir=str(tmp_path),
    )
    import json as _json
    session.metadata["allowed_tools"] = _json.dumps(["Bash(ls *)", "Read"])
    session.metadata["disallowed_tools"] = _json.dumps(["Bash(rm *)"])

    spec = prepare_launch_spec(
        session=session,
        state_dir=tmp_path,
        callback_base_url="http://127.0.0.1:1234",
    )

    # The merged settings should reference --policy-file (not baked-in --allow flags)
    merged = _json.loads(spec.settings_path.read_text(encoding="utf-8"))
    pretool_cmd = merged["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    assert "--policy-file" in pretool_cmd
    assert "--allow " not in pretool_cmd  # rules are in the file, not command args

    # The policy file must exist and contain both allow and disallow rules
    policy_file_path = session.metadata["policy_file"]
    policy = _json.loads(Path(policy_file_path).read_text(encoding="utf-8"))
    assert sorted(policy["allow"]) == ["Bash(ls *)", "Read"]
    assert policy["disallow"] == ["Bash(rm *)"]


def test_prepare_launch_spec_updates_policy_file_on_second_call(tmp_path) -> None:
    """Calling prepare_launch_spec again with changed rules rewrites the file in place."""
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=3600,
        keep_alive=False,
        workdir=str(tmp_path),
    )
    import json as _json
    session.metadata["allowed_tools"] = _json.dumps(["Bash(ls *)"])
    session.metadata["disallowed_tools"] = _json.dumps(["Bash(rm *)"])
    prepare_launch_spec(session=session, state_dir=tmp_path, callback_base_url="http://127.0.0.1:1234")

    # Change rules without restarting
    session.metadata["allowed_tools"] = _json.dumps(["Read", "Write"])
    session.metadata["disallowed_tools"] = ""
    prepare_launch_spec(session=session, state_dir=tmp_path, callback_base_url="http://127.0.0.1:1234")

    policy_file_path = session.metadata["policy_file"]
    policy = _json.loads(Path(policy_file_path).read_text(encoding="utf-8"))
    assert sorted(policy["allow"]) == ["Read", "Write"]
    assert policy["disallow"] == []


def test_prepare_launch_spec_writes_merged_settings_and_metadata(tmp_path) -> None:
    """Normal (non-bypass) launch: pretool hook is injected alongside base broker hook."""
    base_settings = tmp_path / "base.json"
    base_settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "broker"}]}]}}),
        encoding="utf-8",
    )
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=3600,
        keep_alive=False,
        workdir=str(tmp_path),
    )
    session.metadata["settings_path"] = str(base_settings)
    session.metadata["resume_on_launch"] = "True"

    spec = prepare_launch_spec(
        session=session,
        state_dir=tmp_path,
        callback_base_url="http://127.0.0.1:1234",
    )

    assert spec.session_id == "demo"
    assert spec.resume is True
    assert spec.permission_mode == "default"
    # hooks are delivered via --settings <temp-file>, not written into settings.local.json
    assert spec.settings_path is not None
    assert spec.settings_path.name.startswith("claude-settings.merged.")
    assert session.metadata["merged_settings_path"] == str(spec.settings_path)
    assert session.metadata["mcp_config_path"].endswith("mcp-config.json")
    assert session.metadata["mcp_log_path"].endswith("mcp-stdio.log")
    assert session.metadata["claude_stdout_path"].endswith("claude.stdout.log")
    assert session.metadata["claude_stderr_path"].endswith("claude.stderr.log")
    merged = json.loads(spec.settings_path.read_text(encoding="utf-8"))
    mcp_config = json.loads(spec.mcp_config_path.read_text(encoding="utf-8"))
    assert "PreToolUse" in merged["hooks"]
    assert "Stop" in merged["hooks"]
    # base_settings "broker" hook must be preserved
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "broker"
    assert "poor_claude.hooks.pretool_hook" in merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"]
    assert "http://127.0.0.1:1234/hook/stop" in merged["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "poor-claude" in mcp_config["mcpServers"]
    env = mcp_config["mcpServers"]["poor-claude"]["env"]
    assert env["POOR_CLAUDE_CONTROL_URL"] == "http://127.0.0.1:1234"
    assert env["POOR_CLAUDE_ROUTE_KEY"] == session.route_key


def test_prepare_launch_spec_skips_pretool_hook_in_bypass_mode(tmp_path) -> None:
    """bypassPermissions launch: poor-claude's pretool hook is NOT injected."""
    base_settings = tmp_path / "base.json"
    base_settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "broker"}]}]}}),
        encoding="utf-8",
    )
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=3600,
        keep_alive=False,
        workdir=str(tmp_path),
    )
    session.metadata["settings_path"] = str(base_settings)
    session.metadata["permission_mode"] = "bypassPermissions"

    spec = prepare_launch_spec(
        session=session,
        state_dir=tmp_path,
        callback_base_url="http://127.0.0.1:1234",
    )

    assert spec.permission_mode == "bypassPermissions"
    merged = json.loads(spec.settings_path.read_text(encoding="utf-8"))
    # Base broker hook preserved, but poor-claude's pretool hook NOT added
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "broker"
    pretool_commands = [e["hooks"][0]["command"] for e in merged["hooks"]["PreToolUse"]]
    assert not any("poor_claude.hooks.pretool_hook" in cmd for cmd in pretool_commands)
    # Stop hook still injected
    assert "Stop" in merged["hooks"]


def test_prepare_launch_spec_writes_mcp_config_to_route_dir(tmp_path) -> None:
    """MCP config is written to the per-session route directory, not the project workdir."""
    registry = SessionRegistry()
    session = registry.create_or_get(
        session_id="demo",
        ttl_seconds=3600,
        keep_alive=False,
        workdir=str(tmp_path),
    )

    spec = prepare_launch_spec(
        session=session,
        state_dir=tmp_path / "state",
        callback_base_url="http://127.0.0.1:1234",
    )

    # MCP config must be inside the route dir, NOT the project workdir
    assert not (tmp_path / ".mcp.json").exists(), "project workdir .mcp.json must not be written"
    assert spec.mcp_config_path.exists()
    assert spec.mcp_config_path.name == "mcp-config.json"
    assert spec.mcp_config_path.parent != tmp_path  # not in workdir root
    mcp_config = json.loads(spec.mcp_config_path.read_text(encoding="utf-8"))
    assert "poor-claude" in mcp_config["mcpServers"]
    env = mcp_config["mcpServers"]["poor-claude"]["env"]
    assert env["POOR_CLAUDE_ROUTE_KEY"] == session.route_key
    assert env["POOR_CLAUDE_CONTROL_URL"] == "http://127.0.0.1:1234"

import json
from io import StringIO

from poor_claude.hooks import pretool_hook
from poor_claude.hooks.pretool_hook import (
    _collect_allow_rules,
    _parse_rule,
    _read_policy_file,
    _tool_is_allowed,
)


# ---------------------------------------------------------------------------
# _parse_rule
# ---------------------------------------------------------------------------


def test_parse_rule_bare_tool_name() -> None:
    assert _parse_rule("Bash") == ("Bash", None)


def test_parse_rule_with_simple_pattern() -> None:
    assert _parse_rule("Bash(ls *)") == ("Bash", "ls *")


def test_parse_rule_with_complex_pattern() -> None:
    assert _parse_rule("Bash(python -m pytest tests/ -x -q)") == (
        "Bash",
        "python -m pytest tests/ -x -q",
    )


def test_parse_rule_pattern_with_nested_parens() -> None:
    tool, pattern = _parse_rule("Bash(python3 -c 'print(1)')")
    assert tool == "Bash"
    assert pattern == "python3 -c 'print(1)'"


# ---------------------------------------------------------------------------
# _tool_is_allowed
# ---------------------------------------------------------------------------


def test_tool_is_allowed_bare_rule_allows_any_input() -> None:
    assert _tool_is_allowed("Bash", {"command": "ls"}, ["Bash"])


def test_tool_is_allowed_glob_matches_command() -> None:
    assert _tool_is_allowed("Bash", {"command": "ls /tmp"}, ["Bash(ls *)"])


def test_tool_is_allowed_glob_does_not_match_different_command() -> None:
    assert not _tool_is_allowed("Bash", {"command": "rm -rf /"}, ["Bash(ls *)"])


def test_tool_is_allowed_bare_command_not_matched_by_glob_with_required_suffix() -> None:
    # "ls" alone does not match "ls *" (space + wildcard requires something after ls)
    assert not _tool_is_allowed("Bash", {"command": "ls"}, ["Bash(ls *)"])


def test_tool_is_allowed_wrong_tool_name() -> None:
    assert not _tool_is_allowed("Read", {"file_path": "/tmp/f"}, ["Bash(ls *)"])


def test_tool_is_allowed_returns_false_for_empty_rules() -> None:
    assert not _tool_is_allowed("Bash", {"command": "ls"}, [])


def test_tool_is_allowed_read_bare_rule() -> None:
    assert _tool_is_allowed("Read", {"file_path": "/any/file"}, ["Read"])


def test_tool_is_allowed_skill_exact_match() -> None:
    assert _tool_is_allowed(
        "Skill", {"skill": "text-to-speech"}, ["Skill(text-to-speech)"]
    )


def test_tool_is_allowed_skill_glob() -> None:
    assert _tool_is_allowed("Skill", {"skill": "text-to-speech"}, ["Skill(text-*)"])


def test_tool_is_allowed_real_global_settings_rules() -> None:
    rules = [
        "Bash(python3 *.claude/skills/*.py *)",
        "Bash(tts-cli.py *)",
        "Bash(nagori *)",
    ]
    assert _tool_is_allowed(
        "Bash",
        {"command": "python3 /home/user/.claude/skills/tts.py hi"},
        rules,
    )
    assert _tool_is_allowed("Bash", {"command": "tts-cli.py --voice Yue hello"}, rules)
    assert _tool_is_allowed("Bash", {"command": "nagori inject-context"}, rules)
    assert not _tool_is_allowed("Bash", {"command": "rm -rf /"}, rules)


# ---------------------------------------------------------------------------
# _collect_allow_rules
# ---------------------------------------------------------------------------


def test_collect_allow_rules_reads_from_given_paths(tmp_path, monkeypatch) -> None:
    global_settings = tmp_path / "global.json"
    global_settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls *)", "Read"]}})
    )
    project_settings = tmp_path / "project.json"
    project_settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(pytest *)"]}})
    )

    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._settings_file_paths",
        lambda cwd: [global_settings, project_settings],
    )

    rules = _collect_allow_rules("/tmp")
    assert rules == ["Bash(ls *)", "Read", "Bash(pytest *)"]


def test_collect_allow_rules_skips_missing_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._settings_file_paths",
        lambda cwd: [tmp_path / "nonexistent.json"],
    )
    assert _collect_allow_rules("/tmp") == []


def test_collect_allow_rules_skips_invalid_json(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._settings_file_paths",
        lambda cwd: [bad],
    )
    assert _collect_allow_rules("/tmp") == []


def test_collect_allow_rules_skips_non_list_allow(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "s.json"
    settings.write_text(json.dumps({"permissions": {"allow": "Bash"}}))
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._settings_file_paths",
        lambda cwd: [settings],
    )
    assert _collect_allow_rules("/tmp") == []


def test_settings_file_paths_includes_four_locations(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook.Path.home", lambda: tmp_path / "home"
    )
    from poor_claude.hooks.pretool_hook import _settings_file_paths

    paths = _settings_file_paths("/cwd")
    names = [p.name for p in paths]
    assert names == [
        "settings.json",
        "settings.local.json",
        "settings.json",
        "settings.local.json",
    ]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_pretool_hook_allows_tool_matching_allow_list(monkeypatch) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: ["Bash(ls *)"],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls /tmp"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main([]) == 0


def test_pretool_hook_denies_tool_not_in_allow_list(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main([]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_allows_in_bypass_permissions_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},'
            '"permission_mode":"bypassPermissions","cwd":"/tmp"}'
        ),
    )
    assert pretool_hook.main([]) == 0


def test_pretool_hook_allows_in_auto_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},'
            '"permission_mode":"auto","cwd":"/tmp"}'
        ),
    )
    assert pretool_hook.main([]) == 0


def test_pretool_hook_allows_in_dont_ask_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            '{"tool_name":"Bash","tool_input":{"command":"anything"},'
            '"permission_mode":"dontAsk","cwd":"/tmp"}'
        ),
    )
    assert pretool_hook.main([]) == 0


def test_pretool_hook_applies_deny_by_default_in_default_mode(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls"},"permission_mode":"default","cwd":"/tmp"}'),
    )
    assert pretool_hook.main([]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_denies_skill_not_in_allow_list(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            '{"tool_name":"Skill","tool_input":{"skill":"text-to-speech"},"cwd":"/tmp"}'
        ),
    )
    assert pretool_hook.main([]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_disallow_overrides_allow_list(monkeypatch, capsys) -> None:
    """A disallow rule takes priority even when the same tool is in the allow list."""
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: ["Bash"],  # bare allow for all Bash
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--disallow", "Bash(rm *)"]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_disallow_does_not_block_unmatched_tool(monkeypatch) -> None:
    """A disallow rule for rm does not block ls."""
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: ["Bash"],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls /tmp"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--disallow", "Bash(rm *)"]) == 0


# ---------------------------------------------------------------------------
# _read_policy_file
# ---------------------------------------------------------------------------


def test_read_policy_file_returns_allow_and_disallow(tmp_path) -> None:
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(
        json.dumps({"allow": ["Bash(ls *)", "Read"], "disallow": ["Bash(rm *)"]}),
        encoding="utf-8",
    )
    allow, disallow = _read_policy_file(str(policy_file))
    assert allow == ["Bash(ls *)", "Read"]
    assert disallow == ["Bash(rm *)"]


def test_read_policy_file_returns_empty_for_missing_file(tmp_path) -> None:
    allow, disallow = _read_policy_file(str(tmp_path / "nonexistent.json"))
    assert allow == []
    assert disallow == []


def test_read_policy_file_returns_empty_for_invalid_json(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    allow, disallow = _read_policy_file(str(bad))
    assert allow == []
    assert disallow == []


def test_read_policy_file_skips_non_string_entries(tmp_path) -> None:
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(
        json.dumps({"allow": ["Bash", 42, None, "Read"], "disallow": [1, "Bash(rm *)"]}),
        encoding="utf-8",
    )
    allow, disallow = _read_policy_file(str(policy_file))
    assert allow == ["Bash", "Read"]
    assert disallow == ["Bash(rm *)"]


def test_read_policy_file_handles_missing_keys(tmp_path) -> None:
    """A policy file with only 'allow' key returns empty disallow list."""
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(json.dumps({"allow": ["Read"]}), encoding="utf-8")
    allow, disallow = _read_policy_file(str(policy_file))
    assert allow == ["Read"]
    assert disallow == []


def test_pretool_hook_allows_tool_matching_extra_allow_rule(monkeypatch) -> None:
    """--allow rules passed directly to the hook supplement settings-file rules."""
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],  # no rules from settings files
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls /tmp"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--allow", "Bash(ls *)"]) == 0


def test_pretool_hook_denies_when_extra_rule_does_not_match(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--allow", "Bash(ls *)"]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_allows_bare_tool_rule_via_extra_allow(monkeypatch) -> None:
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Read","tool_input":{"file_path":"/any"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--allow", "Read"]) == 0


def test_pretool_hook_allows_tool_matching_policy_file(tmp_path, monkeypatch) -> None:
    """Rules written to a policy file are read fresh each invocation via --policy-file."""
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(
        json.dumps({"allow": ["Bash(ls *)"], "disallow": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"ls /tmp"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--policy-file", str(policy_file)]) == 0


def test_pretool_hook_denies_when_policy_file_does_not_match(tmp_path, monkeypatch, capsys) -> None:
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(
        json.dumps({"allow": ["Bash(ls *)"], "disallow": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--policy-file", str(policy_file)]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_policy_file_disallow_overrides_allow(tmp_path, monkeypatch, capsys) -> None:
    """disallow rules in policy file take priority over allow rules."""
    policy_file = tmp_path / "tools-policy.json"
    policy_file.write_text(
        json.dumps({"allow": ["Bash"], "disallow": ["Bash(rm *)"]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"cwd":"/tmp"}'),
    )
    assert pretool_hook.main(["--policy-file", str(policy_file)]) == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_hook_logs_raw_payload(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "pretool.log"
    monkeypatch.setattr(
        "poor_claude.hooks.pretool_hook._collect_allow_rules",
        lambda cwd: [],
    )
    monkeypatch.setattr(
        "sys.stdin",
        StringIO('{"tool_name":"Skill","tool_input":{"skill":"text-to-speech"}}'),
    )
    assert pretool_hook.main(["--log-path", str(log_path)]) == 2
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert '"tool_name":"Skill"' in record["raw"]

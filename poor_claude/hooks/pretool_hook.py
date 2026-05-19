"""Claude Code PreToolUse hook helper for poor-claude."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from pathlib import Path


DENY_MESSAGE = (
    "poor-claude denied this tool call by default. "
    "Allow it explicitly in Claude settings if this tool should run under claude-no-p."
)


def deny_payload() -> dict[str, object]:
    return {
        "hookSpecificOutput": {"permissionDecision": "deny"},
        "systemMessage": DENY_MESSAGE,
    }


def _append_log(log_path: str | None, raw: str) -> None:
    if not log_path:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "raw": raw}, sort_keys=True) + "\n")


def _settings_file_paths(cwd: str) -> list[Path]:
    """Return the ordered list of Claude settings files to check for allow rules."""
    home = Path.home()
    cwd_path = Path(cwd)
    return [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        cwd_path / ".claude" / "settings.json",
        cwd_path / ".claude" / "settings.local.json",
    ]


def _collect_allow_rules(cwd: str) -> list[str]:
    """Collect all permissions.allow rules from the Claude settings hierarchy."""
    rules: list[str] = []
    for path in _settings_file_paths(cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        allow = data.get("permissions", {}).get("allow", [])
        if isinstance(allow, list):
            rules.extend(r for r in allow if isinstance(r, str))
    return rules


_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")


def _parse_rule(rule: str) -> tuple[str, str | None]:
    """Parse 'ToolName(pattern)' into (tool_name, pattern), or 'ToolName' into (tool_name, None)."""
    m = _RULE_RE.match(rule)
    if m:
        return m.group(1), m.group(2)
    return rule.strip(), None


def _primary_input_string(tool_name: str, tool_input: dict) -> str | None:
    """Return the string to match against a permission rule pattern.

    Returns None for tools without a natural primary string input (e.g. Agent,
    TodoRead) or for tools not yet listed here.  A rule like ``ToolName`` (no
    pattern) still matches via _tool_is_allowed's ``pattern is None`` path, so
    bare-name allow/deny rules work for every tool.  Pattern rules (e.g.
    ``ToolName(glob)``) only work for tools whose primary input is returned here;
    for unknown tools with a pattern, the rule silently never matches.
    """
    if tool_name == "Bash":
        return tool_input.get("command")
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"):
        return (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("pattern")
        )
    if tool_name in ("NotebookEdit", "NotebookRead"):
        return tool_input.get("notebook_path")
    if tool_name == "Skill":
        return tool_input.get("skill") or tool_input.get("name")
    if tool_name == "WebFetch":
        return tool_input.get("url")
    if tool_name == "WebSearch":
        return tool_input.get("query")
    if tool_name == "Agent":
        return tool_input.get("subagent_type")
    return None


def _tool_is_allowed(tool_name: str, tool_input: dict, rules: list[str]) -> bool:
    """Return True if a tool call matches any allow rule."""
    for rule in rules:
        rule_tool, pattern = _parse_rule(rule)
        if rule_tool != tool_name:
            continue
        if pattern is None:
            return True  # bare tool name: allow any call to this tool
        primary = _primary_input_string(tool_name, tool_input)
        if primary is not None and fnmatch.fnmatch(primary, pattern):
            return True
    return False


def _read_policy_file(path: str) -> tuple[list[str], list[str]]:
    """Read allow and disallow rules from a policy JSON file written by the control server.

    Returns (allow_rules, disallow_rules). The file format is::

        {"allow": ["Bash(ls *)", "Read"], "disallow": ["Bash(rm *)"]}
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            allow = [r for r in data.get("allow", []) if isinstance(r, str)]
            disallow = [r for r in data.get("disallow", []) if isinstance(r, str)]
            return allow, disallow
    except (OSError, json.JSONDecodeError):
        pass
    return [], []



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poor-claude-managed", action="store_true")
    parser.add_argument("--log-path")
    parser.add_argument("--allow", action="append", dest="extra_allow_rules", default=[],
                        metavar="RULE", help="Static allow rule baked in at launch (e.g. 'Bash(ls *)')")
    parser.add_argument("--disallow", action="append", dest="extra_disallow_rules", default=[],
                        metavar="RULE", help="Static disallow rule baked in at launch (takes priority over allow)")
    parser.add_argument("--policy-file", metavar="PATH",
                        help="Path to a JSON policy file with allow/disallow rules; read fresh on every invocation")
    args = parser.parse_args(argv)
    raw = sys.stdin.read() or "{}"
    _append_log(args.log_path, raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"poor-claude pretool hook failed: {exc}", file=sys.stderr)
        return 1

    permission_mode = payload.get("permission_mode") or "default"
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or ""

    # In any non-default permission mode, Claude's own logic handles permissions
    # without showing blocking interactive dialogs:
    #   bypassPermissions — all tools auto-approved
    #   auto             — safe ops auto-approved, unsafe auto-denied
    #   acceptEdits      — file edits auto-approved, others auto-denied
    #   dontAsk          — all ops auto-denied without asking
    #   plan             — no tools actually execute
    # Only in "default" mode does Claude show an interactive dialog that would
    # block forever in a headless session — that's where we must step in.
    if permission_mode != "default":
        return 0

    # Collect rules from all sources.
    # Policy file (--policy-file) is read fresh on every invocation so the control
    # server can update rules at runtime without restarting Claude.
    file_allow: list[str] = []
    file_disallow: list[str] = []
    if args.policy_file:
        file_allow, file_disallow = _read_policy_file(args.policy_file)

    allow_rules = _collect_allow_rules(cwd) + list(args.extra_allow_rules) + file_allow
    disallow_rules = list(args.extra_disallow_rules) + file_disallow

    # Disallow takes priority: if the tool matches any disallow rule, deny immediately.
    if _tool_is_allowed(tool_name, tool_input, disallow_rules):
        print(json.dumps(deny_payload()), file=sys.stderr)
        return 2

    if _tool_is_allowed(tool_name, tool_input, allow_rules):
        return 0  # matches allow list: defer to Claude's own check

    # Not in allow list: deny immediately to prevent blocking on a permission dialog
    print(json.dumps(deny_payload()), file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

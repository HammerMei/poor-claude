"""Local-scope Claude Code settings generation."""

from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeneratedSettings:
    path: Path
    data: dict


def _safe_path_name(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(path.resolve()))


def stop_hook_command(callback_url: str) -> str:
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(project_root)
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    return " ".join(
        [
            f"PYTHONPATH={shlex.quote(pythonpath)}",
            shlex.quote(sys.executable),
            "-m",
            "poor_claude.hooks.stop_hook",
            "--poor-claude-managed",
            "--callback-url",
            shlex.quote(callback_url),
        ]
    )


def subagent_stop_hook_command(callback_url: str) -> str:
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(project_root)
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    return " ".join(
        [
            f"PYTHONPATH={shlex.quote(pythonpath)}",
            shlex.quote(sys.executable),
            "-m",
            "poor_claude.hooks.subagent_stop_hook",
            "--poor-claude-managed",
            "--callback-url",
            shlex.quote(callback_url),
        ]
    )


def posttool_hook_command(callback_url: str) -> str:
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(project_root)
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    return " ".join(
        [
            f"PYTHONPATH={shlex.quote(pythonpath)}",
            shlex.quote(sys.executable),
            "-m",
            "poor_claude.hooks.posttool_hook",
            "--poor-claude-managed",
            "--callback-url",
            shlex.quote(callback_url),
        ]
    )


def pretool_hook_command(
    log_path: Path | None = None,
    extra_allow_rules: list[str] | None = None,
    extra_disallow_rules: list[str] | None = None,
    policy_file: Path | None = None,
) -> str:
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(project_root)
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    command = [
        f"PYTHONPATH={shlex.quote(pythonpath)}",
        shlex.quote(sys.executable),
        "-m",
        "poor_claude.hooks.pretool_hook",
        "--poor-claude-managed",
    ]
    if log_path is not None:
        command += ["--log-path", shlex.quote(str(log_path))]
    for rule in extra_allow_rules or []:
        command += ["--allow", shlex.quote(rule)]
    for rule in extra_disallow_rules or []:
        command += ["--disallow", shlex.quote(rule)]
    if policy_file is not None:
        command += ["--policy-file", shlex.quote(str(policy_file))]
    return " ".join(command)


def build_settings(
    callback_url: str,
    *,
    permission_log_path: Path | None = None,
    include_pretool_hook: bool = True,
    extra_pretool_allow_rules: list[str] | None = None,
    extra_pretool_disallow_rules: list[str] | None = None,
    policy_file: Path | None = None,
    agent_launched_url: str | None = None,
    subagent_stop_url: str | None = None,
) -> dict:
    """Build settings that are passed via `claude --settings <local-file>`.

    When *agent_launched_url* is provided a PostToolUse(Agent) hook notifies the
    control server when a background Agent tool call completes its launch turn
    (the PostToolUse payload carries the agentId), and a SubagentStop hook
    notifies the server when a subagent session ends.  Together these allow the
    Stop hook to wait for all background agents to finish before signalling ACG
    that a request is complete (fixes the premature-completion bug when Claude
    uses ``Agent(run_in_background=True)``).

    *subagent_stop_url* is the URL for the SubagentStop hook endpoint.  When
    omitted it is derived from *agent_launched_url* by replacing
    ``/hook/agent-launched`` with ``/hook/subagent-stop``; pass it explicitly
    to avoid that string surgery.
    """
    hooks: dict = {}
    if include_pretool_hook:
        log_path = permission_log_path.parent / "pretool-hook.log" if permission_log_path is not None else None
        hooks["PreToolUse"] = [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": pretool_hook_command(
                            log_path,
                            extra_allow_rules=extra_pretool_allow_rules,
                            extra_disallow_rules=extra_pretool_disallow_rules,
                            policy_file=policy_file,
                        ),
                    }
                ],
            }
        ]
    hooks["Stop"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": stop_hook_command(callback_url),
                }
            ]
        }
    ]
    if agent_launched_url is not None:
        if subagent_stop_url is None:
            # Derive from agent_launched_url as a convenience for callers that
            # follow the /hook/agent-launched convention.  Raise early rather than
            # silently wiring the wrong endpoint (str.replace returns the original
            # string unchanged when the substring is absent).
            if "/hook/agent-launched" not in agent_launched_url:
                raise ValueError(
                    f"Cannot derive subagent_stop_url from {agent_launched_url!r}: "
                    "the URL must contain '/hook/agent-launched', "
                    "or pass subagent_stop_url explicitly."
                )
            subagent_stop_url = agent_launched_url.replace("/hook/agent-launched", "/hook/subagent-stop")
        hooks["PostToolUse"] = [
            {
                "matcher": "^Agent$",
                "hooks": [
                    {
                        "type": "command",
                        "command": posttool_hook_command(agent_launched_url),
                    }
                ],
            }
        ]
        hooks["SubagentStop"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": subagent_stop_hook_command(subagent_stop_url),
                    }
                ]
            }
        ]
    return {"hooks": hooks}


def read_settings(path_or_json: str | None) -> dict:
    if not path_or_json:
        return {}
    candidate = Path(path_or_json).expanduser()
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(path_or_json)


def merge_settings(base: dict, poor_claude_settings: dict) -> dict:
    """Merge poor-claude hooks into caller-provided Claude settings."""
    merged = deepcopy(base)
    merged_permissions = merged.setdefault("permissions", {})
    incoming_permissions = poor_claude_settings.get("permissions", {})
    if incoming_permissions:
        if not isinstance(merged_permissions, dict):
            raise ValueError("permissions must be a JSON object")
        for permission_name, permission_entries in incoming_permissions.items():
            existing = merged_permissions.setdefault(permission_name, [])
            if not isinstance(existing, list) or not isinstance(permission_entries, list):
                raise ValueError(f"permissions.{permission_name} must be a list")
            for entry in deepcopy(permission_entries):
                if entry not in existing:
                    existing.append(entry)
    merged_hooks = merged.setdefault("hooks", {})
    for event_name, hook_entries in poor_claude_settings.get("hooks", {}).items():
        existing = merged_hooks.setdefault(event_name, [])
        if not isinstance(existing, list):
            raise ValueError(f"hooks.{event_name} must be a list")
        # Strip stale poor-claude-managed hooks before adding fresh ones.
        # This prevents duplicate stop hook invocations when the base settings
        # already contain poor-claude hooks (e.g. from a previous session or a
        # global ~/.claude/settings.json that was not cleaned up).
        deduplicated: list = []
        for entry in existing:
            if not isinstance(entry, dict):
                deduplicated.append(entry)
                continue
            entry_copy = deepcopy(entry)
            hook_list = entry_copy.get("hooks")
            if isinstance(hook_list, list):
                retained = [h for h in hook_list if not _is_poor_claude_managed_hook(h)]
                if retained:
                    entry_copy["hooks"] = retained
                    deduplicated.append(entry_copy)
                # else: all hooks were poor-claude-managed — drop the whole entry
            else:
                deduplicated.append(entry_copy)
        deduplicated.extend(deepcopy(hook_entries))
        merged_hooks[event_name] = deduplicated
    return merged


def _deep_merge_dicts(base: dict, overlay: dict) -> dict:
    merged = deepcopy(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def _is_poor_claude_managed_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    hook_type = hook.get("type")
    if hook_type == "command":
        command = hook.get("command")
        return isinstance(command, str) and "--poor-claude-managed" in command
    return False


def strip_poor_claude_managed_settings(data: dict) -> dict:
    stripped = deepcopy(data)
    permissions = stripped.get("permissions")
    if isinstance(permissions, dict) and not permissions:
        stripped.pop("permissions", None)
    hooks = stripped.get("hooks")
    if isinstance(hooks, dict):
        for event_name, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            kept_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    kept_entries.append(entry)
                    continue
                entry_copy = deepcopy(entry)
                hook_list = entry_copy.get("hooks")
                if isinstance(hook_list, list):
                    entry_copy["hooks"] = [hook for hook in hook_list if not _is_poor_claude_managed_hook(hook)]
                if entry_copy.get("hooks"):
                    kept_entries.append(entry_copy)
            if kept_entries:
                hooks[event_name] = kept_entries
            else:
                hooks.pop(event_name, None)
        if not hooks:
            stripped.pop("hooks", None)
    return stripped


def write_settings(*, directory: Path, callback_url: str) -> GeneratedSettings:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude-settings.local.json"
    data = build_settings(callback_url, permission_log_path=directory / "permission-hook.log")
    _write_json_atomic(path, data)
    return GeneratedSettings(path=path, data=data)


def write_merged_settings(
    *,
    directory: Path,
    callback_url: str,
    base_settings_path_or_json: str | None = None,
    include_pretool_hook: bool = True,
    extra_pretool_allow_rules: list[str] | None = None,
    extra_pretool_disallow_rules: list[str] | None = None,
    policy_file: Path | None = None,
    agent_launched_url: str | None = None,
    subagent_stop_url: str | None = None,
) -> GeneratedSettings:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude-settings.merged.json"
    # Remove any stale uuid-named merged settings files left by older versions.
    for old in directory.glob("claude-settings.merged.*.json"):
        if old != path:
            old.unlink(missing_ok=True)
    base = read_settings(base_settings_path_or_json)
    data = merge_settings(
        base,
        build_settings(
            callback_url,
            permission_log_path=directory / "permission-hook.log",
            include_pretool_hook=include_pretool_hook,
            extra_pretool_allow_rules=extra_pretool_allow_rules,
            extra_pretool_disallow_rules=extra_pretool_disallow_rules,
            policy_file=policy_file,
            agent_launched_url=agent_launched_url,
            subagent_stop_url=subagent_stop_url,
        ),
    )
    _write_json_atomic(path, data)
    return GeneratedSettings(path=path, data=data)


def write_project_local_settings(
    *,
    project_dir: Path,
    state_dir: Path,
    callback_url: str,
    base_settings_path_or_json: str | None = None,
) -> GeneratedSettings:
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "settings.local.json"
    existing = strip_poor_claude_managed_settings(read_settings(str(path))) if path.exists() else {}
    user_base = read_settings(base_settings_path_or_json) if base_settings_path_or_json else {}
    base = _deep_merge_dicts(existing, user_base) if user_base else existing
    hook_log_dir = state_dir / "project-settings" / _safe_path_name(project_dir)
    hook_log_dir.mkdir(parents=True, exist_ok=True)
    data = merge_settings(base, build_settings(callback_url, permission_log_path=hook_log_dir / "permission-hook.log"))
    _write_json_atomic(path, data)
    return GeneratedSettings(path=path, data=data)


def ensure_skip_dangerous_mode_prompt(global_settings_path: Path | None = None) -> None:
    """Write skipDangerousModePermissionPrompt=true to ~/.claude/settings.json.

    Claude Code shows an interactive confirmation prompt whenever it is started
    with bypass-permissions flags (--dangerously-skip-permissions or
    --permission-mode bypassPermissions).  On a fresh machine the prompt blocks
    every process start until a human accepts it.

    Writing this key to the global user settings is equivalent to having
    accepted the prompt once interactively — exactly what Claude Code itself
    does after the user clicks "Yes, I accept".  Callers that set
    auto_accept_workspace_trust=True have already opted in to unattended bypass
    mode, so pre-writing the key is the correct and reliable approach (vs.
    injecting key sequences into the PTY, which is fragile).
    """
    path = global_settings_path or Path.home() / ".claude" / "settings.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("skipDangerousModePermissionPrompt") is True:
        return  # already set, nothing to do
    data["skipDangerousModePermissionPrompt"] = True
    _write_json_atomic(path, data)


def ensure_workspace_trust(workdir: Path, global_settings_path: Path | None = None) -> None:
    """Write hasTrustDialogAccepted=true for *workdir* to ~/.claude.json.

    Claude Code shows a "Quick safety check: Is this a project you trust?" prompt
    the first time it opens a directory.  Writing ``hasTrustDialogAccepted=true``
    under ``projects[<workdir>]`` in ``~/.claude.json`` (the main user-level state
    file, distinct from ``~/.claude/settings.json``) is equivalent to having
    accepted the prompt once interactively — exactly what Claude Code itself does
    after the user clicks "Yes, I trust this folder".  Callers that set
    ``auto_accept_workspace_trust=True`` have already opted in to unattended mode,
    so pre-writing the key is the correct and reliable approach (vs. injecting key
    sequences into the PTY, which is fragile).

    The project key matches what Claude Code stores: ``process.cwd()`` run inside
    the workdir, which on macOS returns the symlink-resolved path.
    ``os.path.realpath`` is used (not ``os.path.abspath``) to match that behaviour.
    """
    path = global_settings_path or Path.home() / ".claude.json"
    # Compute the project key the same way Claude Code does.  Claude Code writes the
    # key using process.cwd() (run inside the workdir), which returns the real
    # (symlink-resolved) path on macOS — e.g. /private/tmp/foo even if the caller
    # passed /tmp/foo.  os.path.realpath follows symlinks to match that behaviour;
    # os.path.abspath would not and would produce a mismatched key on macOS.
    key = os.path.normpath(os.path.realpath(str(workdir)))
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    project = projects.get(key)
    if not isinstance(project, dict):
        project = {}
        projects[key] = project
    if project.get("hasTrustDialogAccepted") is True:
        return  # already set, nothing to do
    project["hasTrustDialogAccepted"] = True
    _write_json_atomic(path, data)


def cleanup_project_local_settings(project_dir: Path) -> None:
    path = project_dir / ".claude" / "settings.local.json"
    if not path.exists():
        return
    stripped = strip_poor_claude_managed_settings(read_settings(str(path)))
    if stripped:
        _write_json_atomic(path, stripped)
        return
    path.unlink(missing_ok=True)

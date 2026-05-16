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
) -> dict:
    """Build settings that are passed via `claude --settings <local-file>`."""
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
        existing.extend(deepcopy(hook_entries))
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
) -> GeneratedSettings:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"claude-settings.merged.{uuid.uuid4().hex}.json"
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


def cleanup_project_local_settings(project_dir: Path) -> None:
    path = project_dir / ".claude" / "settings.local.json"
    if not path.exists():
        return
    stripped = strip_poor_claude_managed_settings(read_settings(str(path)))
    if stripped:
        _write_json_atomic(path, stripped)
        return
    path.unlink(missing_ok=True)

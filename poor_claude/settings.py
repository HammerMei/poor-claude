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
            "--callback-url",
            shlex.quote(callback_url),
        ]
    )


def build_settings(callback_url: str) -> dict:
    """Build settings that are passed via `claude --settings <local-file>`."""
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": stop_hook_command(callback_url),
                        }
                    ]
                }
            ]
        }
    }


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
    merged_hooks = merged.setdefault("hooks", {})
    for event_name, hook_entries in poor_claude_settings.get("hooks", {}).items():
        existing = merged_hooks.setdefault(event_name, [])
        if not isinstance(existing, list):
            raise ValueError(f"hooks.{event_name} must be a list")
        existing.extend(deepcopy(hook_entries))
    return merged


def write_settings(*, directory: Path, callback_url: str) -> GeneratedSettings:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude-settings.local.json"
    data = build_settings(callback_url)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return GeneratedSettings(path=path, data=data)


def write_merged_settings(
    *,
    directory: Path,
    callback_url: str,
    base_settings_path_or_json: str | None = None,
) -> GeneratedSettings:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"claude-settings.merged.{uuid.uuid4().hex}.json"
    base = read_settings(base_settings_path_or_json)
    data = merge_settings(base, build_settings(callback_url))
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return GeneratedSettings(path=path, data=data)

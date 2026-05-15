"""MCP connection validation helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class McpValidationResult:
    connected: bool
    log_path: Path
    config_path: Path
    events: list[dict]


def build_mcp_config(*, log_path: Path, control_base_url: str | None = None, route_key: str | None = None) -> dict:
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(project_root)
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    env = {"POOR_CLAUDE_MCP_LOG": str(log_path), "POOR_CLAUDE_OWNED": "1", "PYTHONPATH": pythonpath}
    if control_base_url is not None:
        env["POOR_CLAUDE_CONTROL_URL"] = control_base_url.rstrip("/")
    if route_key is not None:
        env["POOR_CLAUDE_ROUTE_KEY"] = route_key
    return {
        "mcpServers": {
            "poor-claude": {
                "command": sys.executable,
                "args": ["-m", "poor_claude.mcp_stdio_server"],
                "env": env,
            }
        }
    }


def validate_mcp_connection(*, workdir: Path, timeout_seconds: int = 120) -> McpValidationResult:
    artifact_dir = workdir / ".poor-claude-validation"
    artifact_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="poor-claude-mcp-") as tmp:
        tmp_path = Path(tmp)
        log_path = artifact_dir / "mcp-stdio.log"
        config_path = artifact_dir / "mcp-config.json"
        log_path.unlink(missing_ok=True)
        config_path.write_text(json.dumps(build_mcp_config(log_path=log_path), indent=2), encoding="utf-8")
        subprocess.run(  # noqa: S603 - intentional local Claude Code validation
            [
                "claude",
                "--mcp-config",
                str(config_path),
                "--strict-mcp-config",
                "--print",
                "Reply with exactly: MCP_OK",
            ],
            cwd=workdir,
            check=True,
            timeout=timeout_seconds,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        events = []
        if log_path.exists():
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
        connected = any(event.get("event") == "server_start" for event in events) and any(
            event.get("message", {}).get("method") == "initialize" for event in events
        )
        return McpValidationResult(
            connected=connected,
            log_path=log_path,
            config_path=config_path,
            events=events,
        )


def main() -> int:
    result = validate_mcp_connection(workdir=Path.cwd())
    print(
        json.dumps(
            {
                "connected": result.connected,
                "log_path": str(result.log_path),
                "config_path": str(result.config_path),
                "event_count": len(result.events),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.connected else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

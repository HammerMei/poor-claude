"""Claude Code launcher helpers."""

from __future__ import annotations

import subprocess
import json
import os
import pty
import threading
from dataclasses import dataclass
from pathlib import Path

from poor_claude.mcp_router import CHANNEL_NAME
from poor_claude.mcp_validation import build_mcp_config
from poor_claude.settings import write_merged_settings
from poor_claude.session import SessionRecord


@dataclass(frozen=True)
class ClaudeLaunchSpec:
    session_id: str
    settings_path: Path
    mcp_config_path: Path
    channel_name: str
    workdir: Path
    dangerously_skip_permissions: bool = False
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    use_pty: bool = True
    auto_accept_workspace_trust: bool = False


def build_claude_command(spec: ClaudeLaunchSpec) -> list[str]:
    command = [
        "claude",
        "--session-id",
        spec.session_id,
        "--settings",
        str(spec.settings_path),
        "--dangerously-load-development-channels",
        f"server:{spec.channel_name}",
    ]
    if spec.dangerously_skip_permissions:
        command.append("--dangerously-skip-permissions")
    return command


def launch_claude(spec: ClaudeLaunchSpec) -> subprocess.Popen:
    if spec.use_pty:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(  # noqa: S603 - intentional local Claude Code process launch
            build_claude_command(spec),
            cwd=spec.workdir,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        process._poor_claude_pty_master_fd = master_fd  # type: ignore[attr-defined]
        if spec.stdout_path:
            thread = threading.Thread(
                target=_drain_pty_to_log,
                args=(master_fd, spec.stdout_path),
                daemon=True,
            )
            thread.start()
            process._poor_claude_pty_thread = thread  # type: ignore[attr-defined]
        return process

    stdout_handle = spec.stdout_path.open("ab") if spec.stdout_path else subprocess.DEVNULL
    stderr_handle = spec.stderr_path.open("ab") if spec.stderr_path else subprocess.DEVNULL
    return subprocess.Popen(  # noqa: S603 - intentional local Claude Code process launch
        build_claude_command(spec),
        cwd=spec.workdir,
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
    )


def prepare_launch_spec(
    *,
    session: SessionRecord,
    state_dir: Path,
    callback_base_url: str,
) -> ClaudeLaunchSpec:
    """Prepare local merged settings and a launch spec for one session route."""
    route_dir = state_dir / "routes" / _safe_route_name(session.route_key)
    merged = write_merged_settings(
        directory=route_dir,
        callback_url=f"{callback_base_url.rstrip('/')}/hook/stop",
        base_settings_path_or_json=session.metadata.get("settings_path") or None,
    )
    mcp_log_path = route_dir / "mcp-stdio.log"
    stdout_path = route_dir / "claude.stdout.log"
    stderr_path = route_dir / "claude.stderr.log"
    mcp_config_path = Path(session.workdir) / ".mcp.json"
    _merge_project_mcp_config(
        path=mcp_config_path,
        config=build_mcp_config(
            log_path=mcp_log_path,
            control_base_url=callback_base_url,
            route_key=session.route_key,
        ),
    )
    session.metadata["merged_settings_path"] = str(merged.path)
    session.metadata["mcp_config_path"] = str(mcp_config_path)
    session.metadata["mcp_log_path"] = str(mcp_log_path)
    session.metadata["claude_stdout_path"] = str(stdout_path)
    session.metadata["claude_stderr_path"] = str(stderr_path)
    session.metadata["channel_name"] = CHANNEL_NAME
    return ClaudeLaunchSpec(
        session_id=session.session_id,
        settings_path=merged.path,
        mcp_config_path=mcp_config_path,
        channel_name=CHANNEL_NAME,
        workdir=Path(session.workdir),
        dangerously_skip_permissions=session.metadata.get("dangerously_skip_permissions") == "True",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        auto_accept_workspace_trust=session.metadata.get("auto_accept_workspace_trust") == "True",
    )


def _safe_route_name(route_key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in route_key)


def _merge_project_mcp_config(*, path: Path, config: dict) -> None:
    existing: dict = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"existing MCP config is not a JSON object: {path}")
    merged = dict(existing)
    merged_servers = dict(merged.get("mcpServers", {}))
    incoming_servers = config.get("mcpServers", {})
    if not isinstance(merged_servers, dict) or not isinstance(incoming_servers, dict):
        raise ValueError("MCP config mcpServers must be a JSON object")
    extra_servers = set(merged_servers) - set(incoming_servers)
    if extra_servers:
        raise ValueError(
            "existing project .mcp.json contains non-poor-claude MCP servers; "
            f"refusing to launch non-isolated dev channel config: {sorted(extra_servers)}"
        )
    for server_name in set(merged_servers) & set(incoming_servers):
        server = merged_servers[server_name]
        owned = isinstance(server, dict) and isinstance(server.get("env"), dict) and server["env"].get(
            "POOR_CLAUDE_OWNED"
        ) == "1"
        if not owned:
            raise ValueError(
                f"existing project .mcp.json already defines MCP server {server_name!r}; "
                "refusing to overwrite a server not generated by poor-claude"
            )
    merged_servers.update(incoming_servers)
    merged["mcpServers"] = merged_servers
    path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")


def _drain_pty_to_log(master_fd: int, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            handle.write(chunk)
            handle.flush()

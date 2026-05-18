"""Claude Code launcher helpers."""

from __future__ import annotations

import subprocess
import json
import os
import pty
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from poor_claude.mcp_router import CHANNEL_NAME
from poor_claude.mcp_validation import build_mcp_config
from poor_claude.settings import cleanup_project_local_settings, ensure_skip_dangerous_mode_prompt, write_merged_settings
from poor_claude.session import SessionRecord


@dataclass(frozen=True)
class ClaudeLaunchSpec:
    session_id: str
    settings_path: Path | None
    mcp_config_path: Path
    channel_name: str
    workdir: Path
    resume: bool = False
    permission_mode: str = "default"
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    use_pty: bool = True
    auto_accept_workspace_trust: bool = False
    effort: str = "medium"
    model: str | None = None
    append_system_prompt: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    add_dirs: list[str] | None = None


def build_claude_command(spec: ClaudeLaunchSpec) -> list[str]:
    command = [
        "claude",
        "--resume" if spec.resume else "--session-id",
        spec.session_id,
        "--effort",
        spec.effort,
        "--dangerously-load-development-channels",
        f"server:{spec.channel_name}",
    ]
    if spec.settings_path is not None:
        command[3:3] = ["--settings", str(spec.settings_path)]
    command += ["--mcp-config", str(spec.mcp_config_path)]
    if spec.model:
        command.append("--model")
        command.append(spec.model)
    for d in spec.add_dirs or []:
        command += ["--add-dir", d]
    if spec.tools is not None:
        command.append("--tools")
        if spec.tools:
            command.extend(spec.tools)
        else:
            command.append("")  # --tools "" disables all tools
    if spec.system_prompt:
        command.append("--system-prompt")
        command.append(spec.system_prompt)
    if spec.append_system_prompt:
        command.append("--append-system-prompt")
        command.append(spec.append_system_prompt)
    if spec.permission_mode and spec.permission_mode != "default":
        command.append("--permission-mode")
        command.append(spec.permission_mode)
    return command


def launch_claude(spec: ClaudeLaunchSpec) -> subprocess.Popen:
    if spec.use_pty:
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(  # noqa: S603 - intentional local Claude Code process launch
                build_claude_command(spec),
                cwd=spec.workdir,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        process._poor_claude_pty_master_fd = master_fd  # type: ignore[attr-defined]
        # Always drain the PTY master FD — if the kernel buffer fills, the child
        # process blocks on any write and the session stalls permanently.
        thread = threading.Thread(
            target=_drain_pty_to_log,
            args=(master_fd, spec.stdout_path, spec.auto_accept_workspace_trust),
            daemon=True,
        )
        thread.start()
        process._poor_claude_pty_thread = thread  # type: ignore[attr-defined]
        return process

    # Open file handles only to pass FDs into Popen; close Python objects immediately
    # after the call since Popen duplicates the underlying FDs via fork/exec.
    stdout_cm = spec.stdout_path.open("ab") if spec.stdout_path else None
    stderr_cm = spec.stderr_path.open("ab") if spec.stderr_path else None
    try:
        return subprocess.Popen(  # noqa: S603 - intentional local Claude Code process launch
            build_claude_command(spec),
            cwd=spec.workdir,
            stdin=subprocess.DEVNULL,
            stdout=stdout_cm if stdout_cm is not None else subprocess.DEVNULL,
            stderr=stderr_cm if stderr_cm is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if stdout_cm is not None:
            stdout_cm.close()
        if stderr_cm is not None:
            stderr_cm.close()


def _parse_tools_metadata(raw: str) -> list[str] | None:
    """Parse tools metadata string → list for ClaudeLaunchSpec, or None if not set.

    Returns None when the caller did not restrict the tool set (Claude uses its default).
    Returns a list (possibly empty, meaning --tools "") when the caller set --tools.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(t) for t in data if isinstance(t, str)]
    except (ValueError, TypeError):
        pass
    return None


def _parse_json_list_metadata(raw: str) -> list[str] | None:
    """Parse a JSON-list metadata string → list, or None if not set."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(t) for t in data if isinstance(t, str)]
    except (ValueError, TypeError):
        pass
    return None


def prepare_launch_spec(
    *,
    session: SessionRecord,
    state_dir: Path,
    callback_base_url: str,
) -> ClaudeLaunchSpec:
    """Prepare local merged settings and a launch spec for one session route."""
    route_dir = state_dir / "routes" / _safe_route_name(session.route_key)
    # Strip any poor-claude hooks that may have been written to the project's
    # settings.local.json by a previous run, so we don't duplicate them.
    cleanup_project_local_settings(Path(session.workdir))
    # Only inject pretool hook in default permission mode. In bypassPermissions mode
    # the hook would just return 0 on every call anyway, so skip the subprocess overhead.
    # (Other non-default modes like auto/acceptEdits still need the hook — they still
    # run in default mode from the hook's perspective until the mode is confirmed
    # in the payload, so we inject it and let the hook's mode check handle it.)
    permission_mode = session.metadata.get("permission_mode") or "default"
    include_pretool_hook = permission_mode != "bypassPermissions"
    auto_accept = session.metadata.get("auto_accept_workspace_trust") == "True"
    if auto_accept:
        # Pre-write skipDangerousModePermissionPrompt=true to ~/.claude/settings.json
        # so Claude Code never shows the interactive bypass-permissions confirmation
        # prompt on this machine.  This is exactly what Claude Code writes after a
        # human clicks "Yes, I accept" — we just do it upfront since the operator
        # opted in by setting auto_accept_workspace_trust=True.
        ensure_skip_dangerous_mode_prompt()
    # Write allow/disallow rules to a fixed path baked into the hook command once at
    # launch. The file is rewritten on every request so rule changes take effect
    # immediately — the hook reads it fresh on each invocation, no restart needed.
    policy_file = route_dir / "tools-policy.json"
    allowed_tools_raw = session.metadata.get("allowed_tools") or ""
    disallowed_tools_raw = session.metadata.get("disallowed_tools") or ""
    allow_rules: list[str] = json.loads(allowed_tools_raw) if allowed_tools_raw else []
    disallow_rules: list[str] = json.loads(disallowed_tools_raw) if disallowed_tools_raw else []
    route_dir.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({"allow": allow_rules, "disallow": disallow_rules}), encoding="utf-8"
    )
    base_url = callback_base_url.rstrip("/")
    merged = write_merged_settings(
        directory=route_dir,
        callback_url=f"{base_url}/hook/stop",
        base_settings_path_or_json=session.metadata.get("settings_path") or None,
        include_pretool_hook=include_pretool_hook,
        policy_file=policy_file,
        agent_started_url=f"{base_url}/hook/agent-started" if include_pretool_hook else None,
        subagent_stop_url=f"{base_url}/hook/subagent-stop" if include_pretool_hook else None,
    )
    mcp_log_path = route_dir / "mcp-stdio.log"
    stdout_path = route_dir / "claude.stdout.log"
    stderr_path = route_dir / "claude.stderr.log"
    # Write MCP config to the per-session route directory — NOT to the project
    # workdir's .mcp.json.  Using the workdir caused the running Claude session
    # to reload its MCP server when a second session updated the shared file,
    # making it consume prompts meant for the new session.
    mcp_config_path = route_dir / "mcp-config.json"
    mcp_config_path.write_text(
        json.dumps(
            build_mcp_config(
                log_path=mcp_log_path,
                control_base_url=callback_base_url,
                route_key=session.route_key,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    session.metadata["merged_settings_path"] = str(merged.path)
    session.metadata["policy_file"] = str(policy_file)
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
        resume=session.metadata.get("resume_on_launch") == "True",
        permission_mode=session.metadata.get("permission_mode") or "default",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        auto_accept_workspace_trust=session.metadata.get("auto_accept_workspace_trust") == "True",
        effort=session.metadata.get("effort") or "medium",
        model=session.metadata.get("model") or None,
        append_system_prompt=session.metadata.get("append_system_prompt") or None,
        system_prompt=session.metadata.get("system_prompt") or None,
        tools=_parse_tools_metadata(session.metadata.get("tools") or ""),
        add_dirs=_parse_json_list_metadata(session.metadata.get("add_dirs") or ""),
    )


def _safe_route_name(route_key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in route_key)


def cleanup_project_mcp_config(project_dir: Path) -> None:
    """Remove poor-claude-owned MCP server entries from the project's .mcp.json.

    Called on session teardown so we don't leave stale entries behind in the
    project's version-controlled .mcp.json.
    """
    path = project_dir / ".mcp.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return
    cleaned = {
        name: srv
        for name, srv in servers.items()
        if not (
            isinstance(srv, dict)
            and isinstance(srv.get("env"), dict)
            and srv["env"].get("POOR_CLAUDE_OWNED") == "1"
        )
    }
    if cleaned == servers:
        return  # nothing owned by poor-claude, nothing to remove
    if not cleaned:
        path.unlink(missing_ok=True)
        return
    data["mcpServers"] = cleaned
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\a]*(?:\a|\x1b\\)|[()][A-Za-z0-9]|[=>][0-9]*[A-Za-z]?)")


def _drain_pty_to_log(master_fd: int, log_path: Path | None, auto_accept_startup_prompts: bool = False) -> None:
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    recent = ""
    accepted: set[str] = set()
    startup_deadline = time.monotonic() + 30.0
    with (log_path.open("ab") if log_path is not None else open(os.devnull, "wb")) as handle:  # noqa: WPS515
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            handle.write(chunk)
            handle.flush()
            if auto_accept_startup_prompts:
                recent = (recent + chunk.decode("utf-8", errors="ignore"))[-12000:]
                plain_recent = _plain_terminal_text(recent)
                if time.monotonic() > startup_deadline or "listening for channel messages" in plain_recent:
                    auto_accept_startup_prompts = False
                    continue
                key_chunks, prompt_name = _startup_acceptance_keys(recent, accepted)
                if key_chunks is not None and prompt_name is not None:
                    try:
                        handle.write(f"\n[poor-claude auto-accept startup prompt: {prompt_name}]\n".encode("utf-8"))
                        handle.flush()
                        for i, key_chunk in enumerate(key_chunks):
                            if i > 0:
                                time.sleep(0.05)
                            os.write(master_fd, key_chunk)
                        accepted.add(prompt_name)
                    except OSError:
                        return


def _startup_acceptance_keys(raw_text: str, accepted: set[str] | None = None) -> tuple[list[bytes] | None, str | None]:
    """Return key chunks to send (with 50 ms between each) to accept a startup prompt.

    Claude Code's interactive selection widgets run inside Ink (React for CLIs), which
    enables Application Cursor Keys mode (\\x1b[?1h) before rendering.  In that mode the
    terminal expects \\x1bOB for Down-arrow instead of the normal-mode \\x1b[B.  Sending
    the wrong sequence causes the keystroke to be ignored and Enter then confirms the
    default selection — so prompts requiring navigation to a non-default option must use
    \\x1bOB, not \\x1b[B.

    We also split the navigation key from the Enter confirmation so each chunk is written
    separately with a 50 ms pause, giving the Ink widget time to process the navigation
    before we confirm.

    Note: the bypass-permissions warning prompt is intentionally NOT handled here.
    Instead, ``ensure_skip_dangerous_mode_prompt()`` writes
    ``skipDangerousModePermissionPrompt=true`` to ``~/.claude/settings.json`` before
    Claude starts, which prevents the prompt from appearing at all.  PTY key injection
    is too fragile for a safety-gate prompt; the settings approach is deterministic.
    """
    accepted = accepted or set()
    text = _plain_terminal_text(raw_text)
    if (
        "new mcp server found" in text
        and "poor-claude" in text
        and "1. use this and all future" in text
        and "2. use this mcp server" in text
        and "3. continue without" in text
        and "enter to confirm" in text
    ):
        if "mcp-server" not in accepted:
            # App-mode Down to select option-2 ("use this server once"), then Enter.
            return [b"\x1bOB", b"\r"], "mcp-server"
    if (
        "warning" in text
        and "loading development channels" in text
        and "1. i am using this for local development" in text
        and "2. exit" in text
        and "enter to confirm" in text
    ):
        if "development-channels" not in accepted:
            # Option-1 is already highlighted by default; just confirm with Enter.
            return [b"\r"], "development-channels"
    return None, None


def _plain_terminal_text(raw_text: str) -> str:
    without_ansi = ANSI_RE.sub(" ", raw_text)
    return " ".join(without_ansi.lower().split())

"""Interactive Claude Channels validation matrix.

Runs short-lived interactive Claude Code sessions under a PTY to determine which
combination of MCP config source and channel flags resolves local development
channels correctly.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import signal
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from poor_claude.mcp_validation import build_mcp_config


@dataclass(frozen=True)
class MatrixCase:
    name: str
    config_source: str  # flag | project
    strict: bool
    channels_arg: str | None
    dev_arg: str | None


CASES = [
    MatrixCase(
        name="flag_strict_both_tagged",
        config_source="flag",
        strict=True,
        channels_arg="server:poor-claude",
        dev_arg="server:poor-claude",
    ),
    MatrixCase(
        name="flag_strict_dev_only_tagged",
        config_source="flag",
        strict=True,
        channels_arg=None,
        dev_arg="server:poor-claude",
    ),
    MatrixCase(
        name="project_dev_only_tagged",
        config_source="project",
        strict=False,
        channels_arg=None,
        dev_arg="server:poor-claude",
    ),
    MatrixCase(
        name="project_channels_only_tagged",
        config_source="project",
        strict=False,
        channels_arg="server:poor-claude",
        dev_arg=None,
    ),
    MatrixCase(
        name="flag_nonstrict_both_tagged",
        config_source="flag",
        strict=False,
        channels_arg="server:poor-claude",
        dev_arg="server:poor-claude",
    ),
    MatrixCase(
        name="project_both_tagged",
        config_source="project",
        strict=False,
        channels_arg="server:poor-claude",
        dev_arg="server:poor-claude",
    ),
    MatrixCase(
        name="flag_strict_dev_untagged",
        config_source="flag",
        strict=True,
        channels_arg="server:poor-claude",
        dev_arg="poor-claude",
    ),
    MatrixCase(
        name="project_dev_untagged",
        config_source="project",
        strict=False,
        channels_arg="server:poor-claude",
        dev_arg="poor-claude",
    ),
]


def run_case(case: MatrixCase, *, base_dir: Path, duration_seconds: float) -> dict[str, Any]:
    case_dir = base_dir / case.name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    workdir = case_dir / "workdir"
    workdir.mkdir()
    log_path = case_dir / "mcp.log"
    output_path = case_dir / "pty.log"
    debug_path = case_dir / "debug.log"
    config_path = case_dir / "mcp-config.json"
    config = build_mcp_config(log_path=log_path)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    if case.config_source == "project":
        (workdir / ".mcp.json").write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")

    command = ["claude", "--debug", "mcp", "--debug-file", str(debug_path), "--session-id", str(uuid.uuid4())]
    if case.config_source == "flag":
        command += ["--mcp-config", str(config_path)]
    if case.strict:
        command += ["--strict-mcp-config"]
    if case.channels_arg:
        command += ["--channels", case.channels_arg]
    if case.dev_arg:
        command += ["--dangerously-load-development-channels", case.dev_arg]

    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(  # noqa: S603 - intentional local validation
        command,
        cwd=workdir,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)

    output = bytearray()
    start = time.time()
    enter_sent_at = {1.0: False, 3.0: False, 5.0: False}
    try:
        while time.time() - start < duration_seconds:
            elapsed = time.time() - start
            for threshold, sent in list(enter_sent_at.items()):
                if not sent and elapsed >= threshold:
                    try:
                        os.write(master_fd, b"\r")
                    except OSError:
                        pass
                    enter_sent_at[threshold] = True
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if process.poll() is not None:
                # Keep draining briefly after exit.
                time.sleep(0.2)
                break
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    output_text = output.decode(errors="replace")
    output_path.write_text(output_text, encoding="utf-8")
    mcp_events = []
    if log_path.exists():
        mcp_events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    has_initialize = any(event.get("message", {}).get("method") == "initialize" for event in mcp_events)
    has_server_stop = any(event.get("event") == "server_stop" for event in mcp_events)
    no_config_error = "no MCP server configured with that name" in output_text
    entries_tagged_error = "entries must be tagged" in output_text
    listening = "Listening for channel messages" in output_text
    return {
        "name": case.name,
        "command": command,
        "returncode": process.returncode,
        "has_initialize": has_initialize,
        "has_server_stop": has_server_stop,
        "listening": listening,
        "no_config_error": no_config_error,
        "entries_tagged_error": entries_tagged_error,
        "mcp_event_count": len(mcp_events),
        "output_path": str(output_path),
        "debug_path": str(debug_path),
        "mcp_log_path": str(log_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--base-dir", default=".poor-claude-validation/channel-matrix")
    args = parser.parse_args()
    base_dir = Path(args.base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    results = [run_case(case, base_dir=base_dir, duration_seconds=args.duration) for case in CASES]
    summary_path = base_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"summary_path": str(summary_path), "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

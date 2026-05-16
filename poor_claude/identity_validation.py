"""Local-only Claude session identity validation harness."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IdentityValidationResult:
    requested_session_id: str
    observed_session_id: str | None
    matched: bool
    hook_payload_path: Path
    settings_path: Path


def build_dump_settings(*, output_path: Path) -> dict:
    python_code = (
        "from pathlib import Path; "
        "import sys; "
        "Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')"
    )
    command = " ".join(
        [
            shlex.quote(sys.executable),
            "-c",
            shlex.quote(python_code),
            shlex.quote(str(output_path)),
        ]
    )
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ]
                }
            ]
        }
    }


def validate_session_identity(
    *,
    session_id: str,
    workdir: Path,
    timeout_seconds: int = 120,
) -> IdentityValidationResult:
    """Run a minimal local-settings validation using Claude Code print mode.

    This validates only Stop hook session identity. It does not validate the
    interactive Channels path yet.
    """
    artifact_dir = workdir / ".poor-claude-validation"
    artifact_dir.mkdir(exist_ok=True)
    final_payload_path = artifact_dir / f"stop-hook-{session_id}.json"
    final_settings_path = artifact_dir / f"settings-{session_id}.json"
    final_debug_path = artifact_dir / f"debug-{session_id}.log"
    final_payload_path.unlink(missing_ok=True)
    final_debug_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="poor-claude-identity-") as tmp:
        tmp_path = Path(tmp)
        hook_payload_path = final_payload_path
        debug_path = tmp_path / "claude-debug.log"
        settings_path = tmp_path / "claude-settings.local.json"
        settings_path.write_text(
            json.dumps(build_dump_settings(output_path=hook_payload_path), indent=2),
            encoding="utf-8",
        )

        command = [
            "claude",
            "--debug",
            "hooks",
            "--debug-file",
            str(debug_path),
            "--session-id",
            session_id,
            "--settings",
            str(settings_path),
            "--print",
            "Reply with exactly: OK",
        ]
        subprocess.run(  # noqa: S603 - intentional local Claude Code validation
            command,
            cwd=workdir,
            check=True,
            timeout=timeout_seconds,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.time() + 10
        while not hook_payload_path.exists() and time.time() < deadline:
            time.sleep(0.05)

        observed_session_id = None
        if hook_payload_path.exists():
            payload = json.loads(hook_payload_path.read_text(encoding="utf-8"))
            raw_session_id = payload.get("session_id")
            observed_session_id = raw_session_id if isinstance(raw_session_id, str) else None

        if debug_path.exists():
            final_debug_path.write_text(debug_path.read_text(encoding="utf-8"), encoding="utf-8")
        final_settings_path.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")

    return IdentityValidationResult(
        requested_session_id=session_id,
        observed_session_id=observed_session_id,
        matched=observed_session_id == session_id,
        hook_payload_path=final_payload_path,
        settings_path=final_settings_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default=str(uuid.uuid4()))
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    result = validate_session_identity(
        session_id=args.session_id,
        workdir=Path(args.workdir).resolve(),
        timeout_seconds=args.timeout,
    )
    print(
        json.dumps(
            {
                "requested_session_id": result.requested_session_id,
                "observed_session_id": result.observed_session_id,
                "matched": result.matched,
                "hook_payload_path": str(result.hook_payload_path),
                "settings_path": str(result.settings_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.matched else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

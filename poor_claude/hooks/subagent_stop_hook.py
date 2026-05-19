"""Claude Code SubagentStop hook callback helper for poor-claude."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poor-claude-managed", action="store_true")
    parser.add_argument("--callback-url", required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            # No session_id means this is a subagent of a subagent (nested),
            # or an unexpected payload. Nothing to notify.
            return 0
        cwd = data.get("cwd")
        agent_id = data.get("agent_id")
        body = json.dumps({
            "session_id": session_id,
            "cwd": cwd if isinstance(cwd, str) else None,
            "agent_id": agent_id if isinstance(agent_id, str) else None,
        }).encode("utf-8")
        request = urllib.request.Request(
            args.callback_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            response.read()
    except Exception as exc:  # pragma: no cover - exercised through subprocess later
        body_text = exc.read().decode() if hasattr(exc, "read") else ""
        detail = f" | body: {body_text}" if body_text else ""
        print(f"poor-claude subagent-stop hook failed: {exc}{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

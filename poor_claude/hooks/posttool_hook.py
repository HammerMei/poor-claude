"""Claude Code PostToolUse hook helper for poor-claude.

Called after Claude runs an Agent tool call.  If the response carries
``isAsync: true`` the call was a background agent launch — we POST the
``agentId`` to the control server so it can register the pending agent
*before* the first (premature) Stop hook fires.

This hook is best-effort: a missed POST means the Stop hook won't defer
for that agent, which is the same failure mode as the old counter-based
design.  We log to stderr so operators can diagnose lost registrations,
but we always return 0 so Claude Code is never blocked.
"""

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
    except json.JSONDecodeError as exc:
        print(f"poor-claude posttool hook: failed to parse payload: {exc}", file=sys.stderr)
        return 0

    tool_name = data.get("tool_name")
    if tool_name != "Agent":
        return 0

    tool_response = data.get("tool_response")
    if not isinstance(tool_response, dict):
        return 0

    # Only background agent launches carry isAsync: true; sync agents do not.
    if tool_response.get("isAsync") is not True:
        return 0

    agent_id = tool_response.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        print("poor-claude posttool hook: background Agent response missing agentId", file=sys.stderr)
        return 0

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    cwd = data.get("cwd")
    try:
        body = json.dumps({
            "session_id": session_id,
            "cwd": cwd if isinstance(cwd, str) else None,
            "agent_id": agent_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            args.callback_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as response:  # noqa: S310
            response.read()
    except Exception as exc:  # noqa: BLE001
        # Log so operators can diagnose lost registrations.  A missed registration
        # means the Stop hook won't defer for this agent — the premature-completion
        # symptom this feature is meant to fix.
        print(f"poor-claude posttool hook: agent-launched notification failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

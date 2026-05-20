"""Claude Code Stop hook callback helper."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StopHookPayload:
    session_id: str
    response: str
    transcript_path: str | None
    request_id: str | None = None
    cwd: str | None = None


def parse_stop_hook_stdin(raw: str) -> StopHookPayload:
    data = json.loads(raw)
    session_id = data.get("session_id")
    response = data.get("last_assistant_message")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Stop hook payload missing session_id")
    if not isinstance(response, str):
        response = ""
    transcript_path = data.get("transcript_path")
    if response == "" and isinstance(transcript_path, str):
        response = read_last_assistant_message(transcript_path)
    request_id = data.get("request_id")
    if not isinstance(request_id, str) and isinstance(transcript_path, str):
        request_id = read_request_id_from_transcript(transcript_path)
    cwd = data.get("cwd")
    return StopHookPayload(
        session_id=session_id,
        response=response,
        transcript_path=transcript_path if isinstance(transcript_path, str) else None,
        request_id=request_id if isinstance(request_id, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
    )


def read_last_assistant_message(transcript_path: str) -> str:
    path = Path(transcript_path)
    if not path.exists():
        return ""
    last_text = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        text = _content_to_text(content)
        if text:
            last_text = text
    return last_text


def read_request_id_from_transcript(transcript_path: str) -> str | None:
    path = Path(transcript_path)
    if not path.exists():
        return None
    pattern = re.compile(r'<poor-claude-request\s+id="([^"]+)">')
    latest_request_id = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            continue
        text = _content_to_text(message.get("content"))
        match = pattern.search(text)
        if match:
            latest_request_id = match.group(1)
    return latest_request_id


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def post_callback(callback_url: str, payload: StopHookPayload) -> None:
    body = json.dumps(
        {
            "session_id": payload.session_id,
            "request_id": payload.request_id,
            "response": payload.response,
            "transcript_path": payload.transcript_path,
            "cwd": payload.cwd,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - localhost POC callback
        response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poor-claude-managed", action="store_true")
    parser.add_argument("--callback-url", required=True)
    args = parser.parse_args(argv)
    try:
        payload = parse_stop_hook_stdin(sys.stdin.read())
        post_callback(args.callback_url, payload)
    except Exception as exc:  # pragma: no cover - exercised through subprocess later
        body = exc.read().decode() if hasattr(exc, "read") else ""
        detail = f" | body: {body}" if body else ""
        print(f"poor-claude stop hook failed: {exc}{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

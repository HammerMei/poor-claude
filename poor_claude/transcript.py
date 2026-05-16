"""Claude transcript polling helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")
MAX_TRANSCRIPT_READ_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TranscriptResponse:
    text: str
    stop_reason: str | None = None


def transcript_candidates(*, session_id: str, workdir: str, projects_dir: Path | None = None) -> list[Path]:
    if not SAFE_SESSION_ID.fullmatch(session_id):
        return []
    root = projects_dir or Path.home() / ".claude" / "projects"
    direct = root / _claude_project_dir_name(workdir) / f"{session_id}.jsonl"
    candidates = [direct]
    if root.exists():
        for path in root.glob(f"**/{session_id}.jsonl"):
            if path not in candidates:
                candidates.append(path)
    return candidates


def read_response_after_request(
    *,
    session_id: str,
    workdir: str,
    request_id: str,
    projects_dir: Path | None = None,
) -> str | None:
    for candidate in transcript_candidates(session_id=session_id, workdir=workdir, projects_dir=projects_dir):
        response = read_response_after_request_from_file(candidate, request_id=request_id)
        if response is not None:
            return response
    return None


def read_response_after_request_from_file(path: Path, *, request_id: str) -> str | None:
    response = read_response_record_after_request_from_file(path, request_id=request_id)
    return None if response is None else response.text


def read_response_record_after_request_from_file(path: Path, *, request_id: str) -> TranscriptResponse | None:
    if not path.exists():
        return None
    seen_request = False
    latest_response = None
    latest_stop_reason = None
    try:
        lines = _read_recent_text(path, max_bytes=MAX_TRANSCRIPT_READ_BYTES).splitlines()
    except OSError:
        return None
    for line in lines:
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _content_to_text(message.get("content"))
        if role == "user" and f'<poor-claude-request id="{request_id}">' in text:
            seen_request = True
            continue
        if seen_request and role == "assistant" and text:
            latest_response = text
            stop_reason = message.get("stop_reason")
            latest_stop_reason = stop_reason if isinstance(stop_reason, str) else None
    if latest_response is None:
        return None
    return TranscriptResponse(text=latest_response, stop_reason=latest_stop_reason)


def _read_recent_text(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _claude_project_dir_name(workdir: str) -> str:
    return "".join("-" if ch == "/" else ch for ch in str(Path(workdir).expanduser().resolve()) if ch != ".")


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

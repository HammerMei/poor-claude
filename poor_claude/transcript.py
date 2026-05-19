"""Claude transcript polling helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")
MAX_TRANSCRIPT_READ_BYTES = 1024 * 1024  # fallback when no offset is known
_BACKGROUND_AGENT_ID_RE = re.compile(r"agentId:\s+([A-Za-z0-9_-]+)")
# Matches the task-runner notification emitted when Bash(run_in_background=True) launches.
_BACKGROUND_TASK_ID_RE = re.compile(r"Command running in background with ID:\s+([a-z0-9]+)\.")
# Step 1: extract an entire <task-notification> block.
_TASK_NOTIFICATION_BLOCK_RE = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)
# Step 2: extract task-id / status from inside a single block (no cross-block leakage).
_TASK_ID_IN_BLOCK_RE = re.compile(r"<task-id>([^<]+)</task-id>")
# Terminal statuses observed in practice: completed, killed, failed, stopped.
# The only known non-terminal status is "running".  We keep an explicit whitelist
# (rather than "anything not running") so that a future unknown status is ignored
# rather than silently treated as terminal — failing loudly is safer here.
_TASK_STATUS_TERMINAL_RE = re.compile(r"<status>(completed|killed|failed|stopped)</status>")


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


def read_response_after_request_from_file(
    path: Path, *, request_id: str, start_offset: int = 0
) -> str | None:
    response = read_response_record_after_request_from_file(
        path, request_id=request_id, start_offset=start_offset
    )
    return None if response is None else response.text


def read_response_record_after_request_from_file(
    path: Path, *, request_id: str, start_offset: int = 0
) -> TranscriptResponse | None:
    """Read the assistant response that follows a poor-claude request marker.

    ``start_offset`` is the byte offset of the transcript file at the time the
    request was sent.  Seeking directly there avoids the 1 MB tail-read limit
    that would otherwise cause responses to be missed in long-running sessions.
    When unknown (e.g. older call sites), pass 0 to fall back to the tail-read.
    """
    if not path.exists():
        return None
    seen_request = False
    latest_response = None
    latest_stop_reason = None
    try:
        if start_offset > 0:
            lines = _read_from_offset(path, start_offset=start_offset).splitlines()
        else:
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


def _read_from_offset(path: Path, *, start_offset: int) -> str:
    """Read from a known byte offset to EOF — no size limit."""
    with path.open("rb") as handle:
        handle.seek(max(0, start_offset))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _read_recent_text(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _claude_project_dir_name(workdir: str) -> str:
    return "".join("-" if ch == "/" else ch for ch in str(Path(workdir).expanduser().resolve()) if ch != ".")


def find_background_agent_ids_in_transcript(
    path: Path, *, request_id: str, start_offset: int = 0
) -> list[str]:
    """Return agentIds of background Agent launches that occurred during *request_id*.

    Scans tool_result content blocks that appear after the
    ``<poor-claude-request id="...">`` marker and extracts agentIds from
    "Async agent launched successfully." messages.  Used by the Stop hook handler
    and the transcript-polling loop to discover background agents when PostToolUse
    does not fire (e.g. in bypassPermissions mode).

    *start_offset* is an optional byte offset into *path* to start reading from
    (for efficiency; the request marker is still used to scope the results
    correctly even when start_offset > 0).
    """
    if not path.exists():
        return []
    seen_request = False
    agent_ids: list[str] = []
    try:
        if start_offset > 0:
            lines = _read_from_offset(path, start_offset=start_offset).splitlines()
        else:
            lines = _read_recent_text(path, max_bytes=MAX_TRANSCRIPT_READ_BYTES).splitlines()
    except OSError:
        return []
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
        content = message.get("content")
        if role == "user":
            # Check for the poor-claude request marker first (may be in the same
            # message as a tool_result — do NOT skip to next line after finding it).
            user_text = _content_to_text(content)
            if f'<poor-claude-request id="{request_id}">' in user_text:
                seen_request = True
            if not seen_request:
                continue
            # Scan tool_result items for background agent launches
            for text in _tool_result_texts(content):
                if "Async agent launched successfully" in text:
                    for match in _BACKGROUND_AGENT_ID_RE.finditer(text):
                        aid = match.group(1)
                        if aid not in agent_ids:
                            agent_ids.append(aid)
    return agent_ids


def find_background_task_ids_in_transcript(
    path: Path, *, request_id: str, start_offset: int = 0
) -> list[str]:
    """Return task IDs of ``Bash(run_in_background=True)`` launches during *request_id*.

    Scans tool_result content blocks appearing after the
    ``<poor-claude-request id="...">`` marker for
    "Command running in background with ID: {task_id}." messages emitted by the
    Claude Code task runner when a shell command is launched asynchronously.

    *start_offset* works the same as in :func:`find_background_agent_ids_in_transcript`.
    """
    if not path.exists():
        return []
    seen_request = False
    task_ids: list[str] = []
    try:
        if start_offset > 0:
            lines = _read_from_offset(path, start_offset=start_offset).splitlines()
        else:
            lines = _read_recent_text(path, max_bytes=MAX_TRANSCRIPT_READ_BYTES).splitlines()
    except OSError:
        return []
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
        content = message.get("content")
        if role == "user":
            user_text = _content_to_text(content)
            if f'<poor-claude-request id="{request_id}">' in user_text:
                seen_request = True
            if not seen_request:
                continue
            for text in _tool_result_texts(content):
                for match in _BACKGROUND_TASK_ID_RE.finditer(text):
                    tid = match.group(1)
                    if tid not in task_ids:
                        task_ids.append(tid)
    return task_ids


def find_completed_task_ids_in_transcript(
    path: Path, *, request_id: str, start_offset: int = 0
) -> list[str]:
    """Return task IDs of Bash background tasks that reached a terminal status during *request_id*.

    Scans user messages after the ``<poor-claude-request id="...">`` marker for
    ``<task-notification>`` blocks whose ``<status>`` is one of the terminal values:
    ``completed``, ``killed``, ``failed``, or ``stopped``.

    Unlike agent tracking (which relies on the ``SubagentStop`` hook), Bash task
    completion is detected entirely from the transcript so callers must call this
    function to discover when tasks finish and remove them from the pending set.

    *start_offset* works the same as in :func:`find_background_agent_ids_in_transcript`.
    """
    if not path.exists():
        return []
    seen_request = False
    task_ids: list[str] = []
    try:
        if start_offset > 0:
            lines = _read_from_offset(path, start_offset=start_offset).splitlines()
        else:
            lines = _read_recent_text(path, max_bytes=MAX_TRANSCRIPT_READ_BYTES).splitlines()
    except OSError:
        return []
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
        if message.get("role") != "user":
            continue
        text = _content_to_text(message.get("content"))
        if not seen_request:
            if f'<poor-claude-request id="{request_id}">' in text:
                seen_request = True
            if not seen_request:
                continue
        for block in _TASK_NOTIFICATION_BLOCK_RE.finditer(text):
            block_text = block.group(0)
            id_match = _TASK_ID_IN_BLOCK_RE.search(block_text)
            if id_match and _TASK_STATUS_TERMINAL_RE.search(block_text):
                tid = id_match.group(1).strip()
                if tid not in task_ids:
                    task_ids.append(tid)
    return task_ids


def _tool_result_texts(content: object) -> list[str]:
    """Extract text strings from tool_result content blocks in a message content list."""
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        item_content = item.get("content")
        if isinstance(item_content, str):
            texts.append(item_content)
        elif isinstance(item_content, list):
            for sub in item_content:
                if isinstance(sub, dict) and sub.get("type") == "text" and isinstance(sub.get("text"), str):
                    texts.append(sub["text"])
    return texts


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

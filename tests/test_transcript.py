import json
from pathlib import Path

import poor_claude.transcript as transcript_module
from poor_claude.transcript import (
    find_background_agent_ids_in_transcript,
    find_background_task_ids_in_transcript,
    find_completed_task_ids_in_transcript,
    read_response_after_request_from_file,
    read_response_record_after_request_from_file,
    transcript_candidates,
)


def test_transcript_candidates_include_sanitized_workdir(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    candidates = transcript_candidates(
        session_id="demo",
        workdir="/tmp/some.project",
        projects_dir=projects_dir,
    )
    expected_name = "".join("-" if ch == "/" else ch for ch in str(Path("/tmp/some.project").resolve()) if ch != ".")
    assert candidates[0] == projects_dir / expected_name / "demo.jsonl"


def test_transcript_candidates_reject_unsafe_session_id(tmp_path) -> None:
    assert transcript_candidates(session_id="../demo", workdir="/tmp", projects_dir=tmp_path) == []
    assert transcript_candidates(session_id="demo*", workdir="/tmp", projects_dir=tmp_path) == []


def test_read_response_after_request_from_file(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "assistant", "content": "old"}}),
                json.dumps(
                    {
                        "message": {
                            "role": "user",
                            "content": '<channel><poor-claude-request id="req1">hello</poor-claude-request></channel>',
                        }
                    }
                ),
                json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "new"}]}}),
            ]
        ),
        encoding="utf-8",
    )
    assert read_response_after_request_from_file(transcript, request_id="req1") == "new"


def test_read_response_after_request_from_file_returns_latest_response(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
                json.dumps({"message": {"role": "assistant", "content": "intermediate"}}),
                json.dumps({"message": {"role": "assistant", "content": "final"}}),
            ]
        ),
        encoding="utf-8",
    )
    assert read_response_after_request_from_file(transcript, request_id="req1") == "final"


def test_read_response_record_after_request_from_file_includes_stop_reason(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
                json.dumps({"message": {"role": "assistant", "content": "final", "stop_reason": "end_turn"}}),
            ]
        ),
        encoding="utf-8",
    )
    response = read_response_record_after_request_from_file(transcript, request_id="req1")
    assert response is not None
    assert response.text == "final"
    assert response.stop_reason == "end_turn"


def test_read_response_record_after_request_from_file_detects_rate_limit_error(tmp_path) -> None:
    # Mirrors a real transcript entry Claude Code writes when the org monthly
    # spend limit is hit: a synthetic assistant message with stop_reason
    # "stop_sequence" (never "end_turn") and top-level error/isApiErrorMessage
    # markers. No interactive /rate-limit-options TUI is shown for this case.
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "You've hit your org's monthly spend limit",
                                }
                            ],
                            "stop_reason": "stop_sequence",
                        },
                        "error": "rate_limit",
                        "isApiErrorMessage": True,
                        "apiErrorStatus": 429,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    response = read_response_record_after_request_from_file(transcript, request_id="req1")
    assert response is not None
    assert response.stop_reason == "stop_sequence"
    assert response.is_rate_limit_error is True


def test_read_response_record_after_request_from_file_stop_sequence_without_error_is_not_rate_limit(
    tmp_path,
) -> None:
    # A plain stop_sequence completion (e.g. a custom stop string) must not be
    # misclassified as a rate-limit error just because it isn't "end_turn".
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": "final",
                            "stop_reason": "stop_sequence",
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    response = read_response_record_after_request_from_file(transcript, request_id="req1")
    assert response is not None
    assert response.is_rate_limit_error is False


def test_read_response_after_request_ignores_missing_response(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
        encoding="utf-8",
    )
    assert read_response_after_request_from_file(transcript, request_id="req1") is None


def _make_tool_result_event(request_id: str | None, tool_result_text: str) -> dict:
    """Build a simulated tool_result user-message transcript event."""
    content: list = []
    if request_id is not None:
        content.append({"type": "text", "text": f'<poor-claude-request id="{request_id}">'})
    content.append({
        "type": "tool_result",
        "tool_use_id": "toolu_test",
        "content": [{"type": "text", "text": tool_result_text}],
    })
    return {"message": {"role": "user", "content": content}}


def test_find_background_agent_ids_returns_id_after_request_marker(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    tool_result_text = (
        "Async agent launched successfully.\n"
        "agentId: abc123def456 (internal ID - do not mention to user.)\n"
        "The agent is working in the background."
    )
    transcript.write_text(
        "\n".join([
            json.dumps(_make_tool_result_event("req1", tool_result_text)),
        ]),
        encoding="utf-8",
    )
    ids = find_background_agent_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["abc123def456"]


def test_find_background_agent_ids_ignores_entries_before_request_marker(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    before_text = (
        "Async agent launched successfully.\n"
        "agentId: old_agent_id\n"
    )
    after_text = (
        "Async agent launched successfully.\n"
        "agentId: new_agent_id\n"
    )
    transcript.write_text(
        "\n".join([
            # Before request marker (different request or pre-session history)
            json.dumps({"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t0",
                 "content": [{"type": "text", "text": before_text}]},
            ]}}),
            # Request marker
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req2">'},
            ]}}),
            # After request marker
            json.dumps({"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": after_text}]},
            ]}}),
        ]),
        encoding="utf-8",
    )
    ids = find_background_agent_ids_in_transcript(transcript, request_id="req2")
    assert ids == ["new_agent_id"]
    assert "old_agent_id" not in ids


def test_find_background_agent_ids_returns_multiple_ids(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    text1 = "Async agent launched successfully.\nagentId: agent_a (internal ID)\n"
    text2 = "Async agent launched successfully.\nagentId: agent_b (internal ID)\n"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req3">'},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": text1}]},
            ]}}),
            json.dumps({"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2",
                 "content": [{"type": "text", "text": text2}]},
            ]}}),
        ]),
        encoding="utf-8",
    )
    ids = find_background_agent_ids_in_transcript(transcript, request_id="req3")
    assert set(ids) == {"agent_a", "agent_b"}


def test_find_background_agent_ids_ignores_non_async_tool_results(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req4">'},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "Agent completed synchronously.\nResult: done"}]},
            ]}}),
        ]),
        encoding="utf-8",
    )
    ids = find_background_agent_ids_in_transcript(transcript, request_id="req4")
    assert ids == []


def test_find_background_agent_ids_handles_string_content(tmp_path) -> None:
    """Also handles tool_result content as a plain string (not a list)."""
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req5">'},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "Async agent launched successfully.\nagentId: str_agent (internal ID)\n"},
            ]}}),
        ]),
        encoding="utf-8",
    )
    ids = find_background_agent_ids_in_transcript(transcript, request_id="req5")
    assert ids == ["str_agent"]


def test_find_background_agent_ids_returns_empty_for_missing_file(tmp_path) -> None:
    ids = find_background_agent_ids_in_transcript(tmp_path / "nonexistent.jsonl", request_id="req1")
    assert ids == []


def test_read_response_after_request_ignores_unreadable_transcript(monkeypatch, tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    def fail_read(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(transcript_module, "_read_recent_text", fail_read)
    assert read_response_after_request_from_file(transcript, request_id="req1") is None


# ---------------------------------------------------------------------------
# find_background_task_ids_in_transcript tests
# ---------------------------------------------------------------------------

def _make_bash_task_launch_event(request_id: str | None, task_id: str) -> dict:
    """Simulate the tool_result user message when a Bash background task is launched."""
    tool_result_text = (
        f"Command running in background with ID: {task_id}. "
        "Output is being written to: /tmp/tasks/output. "
        "You will be notified when it completes."
    )
    content: list = []
    if request_id is not None:
        content.append({"type": "text", "text": f'<poor-claude-request id="{request_id}">'})
    content.append({
        "type": "tool_result",
        "tool_use_id": "toolu_bash",
        "content": [{"type": "text", "text": tool_result_text}],
    })
    return {"message": {"role": "user", "content": content}}


def _make_task_notification_event(task_id: str, status: str) -> dict:
    """Simulate the user message injected when a Bash background task finishes."""
    content = (
        f"<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>toolu_bash</tool-use-id>\n"
        f"<output-file>/tmp/tasks/{task_id}.output</output-file>\n"
        f"<status>{status}</status>\n"
        f"<summary>Background command completed</summary>\n"
        f"</task-notification>"
    )
    return {"message": {"role": "user", "content": content}}


def test_find_background_task_ids_returns_id_after_request_marker(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps(_make_bash_task_launch_event("req1", "bp7l7f22b")),
        ]),
        encoding="utf-8",
    )
    ids = find_background_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["bp7l7f22b"]


def test_find_background_task_ids_ignores_before_request_marker(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps(_make_bash_task_launch_event(None, "boldtask1")),
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req2">'},
            ]}}),
            json.dumps(_make_bash_task_launch_event(None, "bnewtask1")),
        ]),
        encoding="utf-8",
    )
    ids = find_background_task_ids_in_transcript(transcript, request_id="req2")
    assert ids == ["bnewtask1"]
    assert "boldtask1" not in ids


def test_find_background_task_ids_returns_multiple(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps(_make_bash_task_launch_event("req3", "btask1111")),
            json.dumps(_make_bash_task_launch_event(None, "btask2222")),
        ]),
        encoding="utf-8",
    )
    ids = find_background_task_ids_in_transcript(transcript, request_id="req3")
    assert set(ids) == {"btask1111", "btask2222"}


def test_find_background_task_ids_ignores_non_background_tool_results(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": '<poor-claude-request id="req4">'},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "Command ran and exited with code 0."}]},
            ]}}),
        ]),
        encoding="utf-8",
    )
    ids = find_background_task_ids_in_transcript(transcript, request_id="req4")
    assert ids == []


def test_find_background_task_ids_returns_empty_for_missing_file(tmp_path) -> None:
    ids = find_background_task_ids_in_transcript(tmp_path / "nonexistent.jsonl", request_id="req1")
    assert ids == []


# ---------------------------------------------------------------------------
# find_completed_task_ids_in_transcript tests
# ---------------------------------------------------------------------------

def test_find_completed_task_ids_returns_completed_status(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
            json.dumps(_make_task_notification_event("bp7l7f22b", "completed")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["bp7l7f22b"]


def test_find_completed_task_ids_returns_killed_status(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
            json.dumps(_make_task_notification_event("bxx3ro3af", "killed")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["bxx3ro3af"]


def test_find_completed_task_ids_returns_failed_status(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
            json.dumps(_make_task_notification_event("btask001", "failed")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["btask001"]


def test_find_completed_task_ids_returns_stopped_status(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
            json.dumps(_make_task_notification_event("btask002", "stopped")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == ["btask002"]


def test_find_completed_task_ids_ignores_running_status(tmp_path) -> None:
    """'running' is a non-terminal status and must NOT be treated as completion."""
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req1">'}}),
            json.dumps(_make_task_notification_event("btask003", "running")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req1")
    assert ids == []


def test_find_completed_task_ids_ignores_before_request_marker(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps(_make_task_notification_event("boldtask1", "completed")),
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req2">'}}),
            json.dumps(_make_task_notification_event("bnewtask1", "completed")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req2")
    assert ids == ["bnewtask1"]
    assert "boldtask1" not in ids


def test_find_completed_task_ids_returns_multiple(tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req3">'}}),
            json.dumps(_make_task_notification_event("btask1111", "completed")),
            json.dumps(_make_task_notification_event("btask2222", "killed")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req3")
    assert set(ids) == {"btask1111", "btask2222"}


def test_find_completed_task_ids_ignores_unknown_status(tmp_path) -> None:
    """A status not in the whitelist (e.g. 'cancelled') must be silently ignored.

    The whitelist design means an unrecognised status leaves the task in pending
    (request waits until timeout) rather than completing prematurely.  This test
    anchors that intended behaviour so future readers know the silent-ignore is
    deliberate.
    """
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req5">'}}),
            json.dumps(_make_task_notification_event("btask5555", "cancelled")),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req5")
    assert ids == [], "unknown status 'cancelled' should be ignored, not treated as terminal"


def test_find_completed_task_ids_no_cross_block_leakage(tmp_path) -> None:
    """Two <task-notification> blocks in a SINGLE user message must not cross-match.

    A naive single regex can pick up task-id from block 1 and status from block 2.
    The 3-part block-based approach must isolate each block so only the task whose
    own block contains a terminal status is returned.

    Layout:
      block 1: task-id=btask3333, status=running   → must NOT be returned
      block 2: task-id=btask4444, status=completed  → must be returned
    """
    two_blocks = (
        "<task-notification>\n"
        "<task-id>btask3333</task-id>\n"
        "<tool-use-id>toolu_a</tool-use-id>\n"
        "<output-file>/tmp/btask3333.out</output-file>\n"
        "<status>running</status>\n"
        "<summary>still running</summary>\n"
        "</task-notification>\n"
        "<task-notification>\n"
        "<task-id>btask4444</task-id>\n"
        "<tool-use-id>toolu_b</tool-use-id>\n"
        "<output-file>/tmp/btask4444.out</output-file>\n"
        "<status>completed</status>\n"
        "<summary>done</summary>\n"
        "</task-notification>"
    )
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="req4">'}}),
            json.dumps({"message": {"role": "user", "content": two_blocks}}),
        ]),
        encoding="utf-8",
    )
    ids = find_completed_task_ids_in_transcript(transcript, request_id="req4")
    assert ids == ["btask4444"], (
        "btask3333 has status=running so it must not appear; "
        "cross-block regex leakage would incorrectly include it"
    )


def test_find_completed_task_ids_returns_empty_for_missing_file(tmp_path) -> None:
    ids = find_completed_task_ids_in_transcript(tmp_path / "nonexistent.jsonl", request_id="req1")
    assert ids == []

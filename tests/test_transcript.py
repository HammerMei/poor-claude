import json
from pathlib import Path

import poor_claude.transcript as transcript_module
from poor_claude.transcript import find_background_agent_ids_in_transcript, read_response_after_request_from_file, read_response_record_after_request_from_file, transcript_candidates


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

import json
from pathlib import Path

import poor_claude.transcript as transcript_module
from poor_claude.transcript import read_response_after_request_from_file, read_response_record_after_request_from_file, transcript_candidates


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


def test_read_response_after_request_ignores_unreadable_transcript(monkeypatch, tmp_path) -> None:
    transcript = tmp_path / "demo.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    def fail_read(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(transcript_module, "_read_recent_text", fail_read)
    assert read_response_after_request_from_file(transcript, request_id="req1") is None

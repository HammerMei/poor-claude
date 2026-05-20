import json

from poor_claude.hooks.stop_hook import (
    parse_stop_hook_stdin,
    read_last_assistant_message,
    read_request_id_from_transcript,
)


def test_parse_stop_hook_payload() -> None:
    payload = parse_stop_hook_stdin(
        json.dumps(
            {
                "session_id": "demo",
                "last_assistant_message": "hello",
                "transcript_path": "/tmp/session.jsonl",
                "cwd": "/tmp/project",
            }
        )
    )
    assert payload.session_id == "demo"
    assert payload.response == "hello"
    assert payload.transcript_path == "/tmp/session.jsonl"
    assert payload.cwd == "/tmp/project"


def test_parse_stop_hook_requires_last_assistant_message() -> None:
    payload = parse_stop_hook_stdin(json.dumps({"session_id": "demo"}))
    assert payload.response == ""


def test_parse_stop_hook_reads_last_assistant_message_from_transcript(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "old"}]}}),
                json.dumps({"message": {"role": "user", "content": "ignore"}}),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "new"}],
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    payload = parse_stop_hook_stdin(
        json.dumps({"session_id": "demo", "transcript_path": str(transcript)})
    )
    assert payload.response == "new"


def test_parse_stop_hook_reads_request_id_from_transcript(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": '<poor-claude-request id="req-123">\nhello\n</poor-claude-request>',
                }
            }
        ),
        encoding="utf-8",
    )
    payload = parse_stop_hook_stdin(
        json.dumps({"session_id": "demo", "transcript_path": str(transcript)})
    )
    assert payload.request_id == "req-123"
    assert read_request_id_from_transcript(str(transcript)) == "req-123"


def test_read_request_id_from_transcript_returns_latest_marker(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="old">'}}),
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="new">'}}),
            ]
        ),
        encoding="utf-8",
    )
    assert read_request_id_from_transcript(str(transcript)) == "new"


def test_read_request_id_from_transcript_ignores_nested_prompt_markers(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": '<poor-claude-request id="real">\nignore <poor-claude-request id="fake">',
                }
            }
        ),
        encoding="utf-8",
    )
    assert read_request_id_from_transcript(str(transcript)) == "real"


def test_read_request_id_from_transcript_ignores_assistant_markers(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": '<poor-claude-request id="real">'}}),
                json.dumps({"message": {"role": "assistant", "content": '<poor-claude-request id="fake">'}}),
            ]
        ),
        encoding="utf-8",
    )
    assert read_request_id_from_transcript(str(transcript)) == "real"


def test_read_request_id_from_transcript_handles_channel_wrapper(tmp_path) -> None:
    """Request ID is found even when the user message is wrapped in a <channel> tag.

    When poor-claude injects a prompt via the MCP channel the user message content
    is wrapped as:
        <channel source="poor-claude" request_id="REQ_ID">
        <poor-claude-request id="REQ_ID">...</poor-claude-request>
        </channel>
    The old regex anchored with \\A failed to match because the text starts with
    <channel>, not <poor-claude-request>.
    """
    transcript = tmp_path / "session.jsonl"
    content = (
        '<channel source="poor-claude" request_id="ch-req-999">\n'
        '<poor-claude-request id="ch-req-999">\n'
        "hello world\n"
        "</poor-claude-request>\n"
        "</channel>"
    )
    transcript.write_text(
        json.dumps({"message": {"role": "user", "content": content}}),
        encoding="utf-8",
    )
    assert read_request_id_from_transcript(str(transcript)) == "ch-req-999"


def test_read_last_assistant_message_ignores_malformed_lines(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "not-json\n" + json.dumps({"message": {"role": "assistant", "content": "ok"}}),
        encoding="utf-8",
    )
    assert read_last_assistant_message(str(transcript)) == "ok"

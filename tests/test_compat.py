import json

from poor_claude.compat import creation_json, stream_json_lines


def test_creation_json_contains_session_id() -> None:
    assert json.loads(creation_json(session_id="demo")) == {"session_id": "demo"}


def test_stream_json_lines_match_acg_parser_expectations() -> None:
    assistant, result = [json.loads(line) for line in stream_json_lines(session_id="demo", text="hello")]
    assert assistant["type"] == "assistant"
    assert assistant["message"]["content"][0] == {"type": "text", "text": "hello"}
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result["session_id"] == "demo"
    assert result["result"] == "hello"
    assert result["usage"]["input_tokens"] == 0

"""Claude CLI output compatibility helpers."""

from __future__ import annotations

import json
from typing import Any


PLACEHOLDER_RESPONSE = "poor-claude request queued; MCP/Claude launcher not implemented yet."


def creation_json(*, session_id: str) -> str:
    return json.dumps({"session_id": session_id})


def stream_json_lines(
    *,
    session_id: str,
    text: str,
    is_error: bool = False,
    subtype: str = "success",
) -> list[str]:
    assistant_event: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": text,
                }
            ]
        },
    }
    result_event: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "session_id": session_id,
        "result": text,
        "total_cost_usd": 0,
        "duration_ms": 0,
        "num_turns": 1,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    return [json.dumps(assistant_event), json.dumps(result_event)]

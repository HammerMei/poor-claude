import json
from io import StringIO

from poor_claude.mcp_stdio_server import handle_request, main
from poor_claude.mcp_validation import build_mcp_config


def test_mcp_initialize_declares_claude_channel() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert response["id"] == 1
    assert response["result"]["capabilities"] == {"experimental": {"claude/channel": {}}}


def test_mcp_tools_list_empty() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert response == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}


def test_build_mcp_config_uses_local_module(tmp_path) -> None:
    config = build_mcp_config(log_path=tmp_path / "mcp.log")
    server = config["mcpServers"]["poor-claude"]
    assert server["args"] == ["-m", "poor_claude.mcp_stdio_server"]
    assert server["env"]["POOR_CLAUDE_MCP_LOG"].endswith("mcp.log")
    assert server["env"]["POOR_CLAUDE_OWNED"] == "1"
    assert "poor-claude" in server["env"]["PYTHONPATH"]


def test_build_mcp_config_can_include_control_routing(tmp_path) -> None:
    config = build_mcp_config(
        log_path=tmp_path / "mcp.log",
        control_base_url="http://127.0.0.1:1234/",
        route_key="/tmp::demo",
    )
    env = config["mcpServers"]["poor-claude"]["env"]
    assert env["POOR_CLAUDE_CONTROL_URL"] == "http://127.0.0.1:1234"
    assert env["POOR_CLAUDE_ROUTE_KEY"] == "/tmp::demo"


def test_mcp_stdio_server_handles_malformed_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", StringIO("not-json\n"))
    assert main() == 0
    output = capsys.readouterr().out
    response = json.loads(output)
    assert response["error"]["code"] == -32700


def test_mcp_stdio_server_rejects_non_object_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", StringIO("[]\n"))
    assert main() == 0
    output = capsys.readouterr().out
    response = json.loads(output)
    assert response["error"]["code"] == -32700
    assert "must be an object" in response["error"]["message"]

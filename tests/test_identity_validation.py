import sys
from pathlib import Path

from poor_claude.identity_validation import build_dump_settings


def test_build_dump_settings_uses_local_output_file() -> None:
    settings = build_dump_settings(output_path=Path("/tmp/hook.json"))
    hook = settings["hooks"]["Stop"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert sys.executable in hook["command"]
    assert "poor_claude.hooks.dump_stop_hook" in hook["command"]
    assert "/tmp/hook.json" in hook["command"]

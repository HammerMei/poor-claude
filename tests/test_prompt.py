from io import StringIO

import pytest

from poor_claude.prompt import PromptError, resolve_prompt


class FakeStdin(StringIO):
    def __init__(self, value: str = "", *, is_tty: bool = True) -> None:
        super().__init__(value)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_resolve_prompt_from_print_flag() -> None:
    resolved = resolve_prompt(
        print_prompt="hello",
        positional_prompt=None,
        stdin=FakeStdin(is_tty=True),
    )
    assert resolved.prompt == "hello"
    assert resolved.source == "print"


def test_resolve_prompt_from_stdin() -> None:
    resolved = resolve_prompt(
        print_prompt=None,
        positional_prompt=None,
        stdin=FakeStdin("hello", is_tty=False),
    )
    assert resolved.prompt == "hello"
    assert resolved.source == "stdin"


def test_resolve_prompt_rejects_ambiguous_sources() -> None:
    with pytest.raises(PromptError, match="ambiguous"):
        resolve_prompt(
            print_prompt="hello",
            positional_prompt="world",
            stdin=FakeStdin(is_tty=True),
        )


def test_resolve_prompt_rejects_missing_prompt() -> None:
    with pytest.raises(PromptError, match="no prompt"):
        resolve_prompt(
            print_prompt=None,
            positional_prompt=None,
            stdin=FakeStdin(is_tty=True),
        )

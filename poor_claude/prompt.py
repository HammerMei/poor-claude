"""Prompt-source resolution for claude-no-p."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class ResolvedPrompt:
    prompt: str
    source: str


class PromptError(ValueError):
    """Raised when prompt input is missing or ambiguous."""


def resolve_prompt(
    *,
    print_prompt: str | bool | None,
    positional_prompt: str | None,
    stdin: TextIO,
) -> ResolvedPrompt:
    """Resolve exactly one prompt source from -p, positional prompt, or stdin."""
    stdin_text: str | None = None
    if not stdin.isatty():
        stdin_text = stdin.read()
        if stdin_text == "":
            stdin_text = None

    sources = []
    if isinstance(print_prompt, str):
        sources.append(("print", print_prompt))
    if positional_prompt is not None:
        sources.append(("positional", positional_prompt))
    if stdin_text is not None:
        sources.append(("stdin", stdin_text))

    if not sources:
        raise PromptError("no prompt provided")
    if len(sources) > 1:
        names = ", ".join(name for name, _ in sources)
        raise PromptError(f"ambiguous prompt sources: {names}")

    source, prompt = sources[0]
    return ResolvedPrompt(prompt=prompt, source=source)

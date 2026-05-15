"""Configuration helpers for poor-claude."""

from __future__ import annotations

import os
from dataclasses import dataclass


AUTO_SESSION_TTL_SECONDS = 900
NAMED_SESSION_TTL_SECONDS = 3600
TTL_ENV_VAR = "POOR_CLAUDE_TTL_SECONDS"


@dataclass(frozen=True)
class TtlConfig:
    ttl_seconds: int | None
    keep_alive: bool


def parse_ttl(value: str) -> int:
    """Parse a TTL value in seconds."""
    try:
        ttl = int(value)
    except ValueError as exc:
        raise ValueError("TTL must be an integer number of seconds") from exc
    if ttl < 0:
        raise ValueError("TTL must be greater than or equal to 0")
    return ttl


def resolve_ttl(
    *,
    session_id: str | None,
    cli_ttl: str | None,
    keep_alive: bool,
    environ: dict[str, str] | None = None,
    user_default_ttl: int | None = None,
) -> TtlConfig:
    """Resolve effective TTL using documented precedence."""
    if keep_alive and cli_ttl is not None:
        raise ValueError("--ttl and --keep-alive cannot be used together")
    if keep_alive:
        return TtlConfig(ttl_seconds=None, keep_alive=True)

    env = os.environ if environ is None else environ
    if cli_ttl is not None:
        return TtlConfig(ttl_seconds=parse_ttl(cli_ttl), keep_alive=False)
    if TTL_ENV_VAR in env and env[TTL_ENV_VAR] != "":
        return TtlConfig(ttl_seconds=parse_ttl(env[TTL_ENV_VAR]), keep_alive=False)
    if user_default_ttl is not None:
        if user_default_ttl < 0:
            raise ValueError("default_ttl_seconds must be greater than or equal to 0")
        return TtlConfig(ttl_seconds=user_default_ttl, keep_alive=False)

    default_ttl = NAMED_SESSION_TTL_SECONDS if session_id else AUTO_SESSION_TTL_SECONDS
    return TtlConfig(ttl_seconds=default_ttl, keep_alive=False)

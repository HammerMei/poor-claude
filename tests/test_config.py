import pytest

from poor_claude.config import (
    AUTO_SESSION_TTL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    NAMED_SESSION_TTL_SECONDS,
    TIMEOUT_ENV_VAR,
    TTL_ENV_VAR,
    parse_ttl,
    resolve_timeout,
    resolve_ttl,
)


def test_parse_ttl_rejects_negative() -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        parse_ttl("-1")


def test_resolve_ttl_uses_cli_first() -> None:
    ttl = resolve_ttl(
        session_id=None,
        cli_ttl="3",
        keep_alive=False,
        environ={TTL_ENV_VAR: "9"},
    )
    assert ttl.ttl_seconds == 3
    assert ttl.keep_alive is False


def test_resolve_ttl_uses_env_before_default() -> None:
    ttl = resolve_ttl(
        session_id="demo",
        cli_ttl=None,
        keep_alive=False,
        environ={TTL_ENV_VAR: "7"},
    )
    assert ttl.ttl_seconds == 7


def test_resolve_ttl_defaults_by_session_type() -> None:
    auto_ttl = resolve_ttl(session_id=None, cli_ttl=None, keep_alive=False, environ={})
    named_ttl = resolve_ttl(session_id="demo", cli_ttl=None, keep_alive=False, environ={})
    assert auto_ttl.ttl_seconds == AUTO_SESSION_TTL_SECONDS
    assert named_ttl.ttl_seconds == NAMED_SESSION_TTL_SECONDS


def test_resolve_ttl_rejects_keep_alive_with_ttl() -> None:
    with pytest.raises(ValueError, match="cannot be used together"):
        resolve_ttl(session_id=None, cli_ttl="1", keep_alive=True, environ={})


def test_resolve_timeout_uses_cli_first() -> None:
    assert resolve_timeout(cli_timeout=120, environ={TIMEOUT_ENV_VAR: "9999"}) == 120


def test_resolve_timeout_uses_env_before_default() -> None:
    assert resolve_timeout(cli_timeout=None, environ={TIMEOUT_ENV_VAR: "600"}) == 600


def test_resolve_timeout_falls_back_to_default() -> None:
    assert resolve_timeout(cli_timeout=None, environ={}) == DEFAULT_TIMEOUT_SECONDS


def test_resolve_timeout_rejects_non_integer_env() -> None:
    with pytest.raises(ValueError, match="integer number of seconds"):
        resolve_timeout(cli_timeout=None, environ={TIMEOUT_ENV_VAR: "abc"})


def test_resolve_timeout_rejects_non_positive_env() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        resolve_timeout(cli_timeout=None, environ={TIMEOUT_ENV_VAR: "0"})

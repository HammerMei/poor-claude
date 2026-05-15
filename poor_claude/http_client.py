"""Small JSON HTTP client for the local control daemon."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HttpClientError(RuntimeError):
    pass


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost POC daemon
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise HttpClientError(f"{method} {url} failed: {exc.code} {detail}") from exc
    if raw == "":
        return {}
    return json.loads(raw)

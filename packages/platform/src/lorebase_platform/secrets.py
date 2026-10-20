"""Secret references and the registry of secret values used for log redaction.

A secret setting holds either a literal value or a reference resolved at load time:

- `secret://env/NAME`: value of the environment variable `NAME`;
- `secret://file/PATH`: content of the file at `PATH` (trailing newline removed). Use a double
  slash for an absolute path, for example `secret://file//run/secrets/db_password`.

Resolved values are typed `SecretStr` and are registered so that the logging redaction filter
can mask them wherever they appear.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, SecretStr

REFERENCE_PREFIX = "secret://"
_REFERENCE = re.compile(r"^secret://(?P<provider>[a-z0-9_-]+)/(?P<target>.+)$")


class SecretResolutionError(ValueError):
    """A secret reference cannot be resolved. The message never contains a secret value."""


def _from_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise SecretResolutionError(f"environment variable {name!r} is not set")
    return value


def _from_file(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretResolutionError(f"cannot read secret file {path!r}: {exc.strerror}") from None
    return content.removesuffix("\n").removesuffix("\r")


_PROVIDERS: dict[str, Callable[[str], str]] = {"env": _from_env, "file": _from_file}

_known_values: set[str] = set()
_lock = threading.Lock()


def register_secret_value(value: str) -> None:
    """Remember a secret value so log redaction can mask it."""
    if value:
        with _lock:
            _known_values.add(value)


def registered_secret_values() -> frozenset[str]:
    """Snapshot of every secret value seen so far."""
    with _lock:
        return frozenset(_known_values)


def resolve_secret(value: str) -> str:
    """Return the secret behind a reference; a literal value is returned unchanged."""
    if not value.startswith(REFERENCE_PREFIX):
        register_secret_value(value)
        return value
    match = _REFERENCE.match(value)
    if match is None:
        raise SecretResolutionError(
            f"malformed secret reference {value!r}; expected secret://env/NAME or "
            "secret://file/PATH"
        )
    provider = _PROVIDERS.get(match["provider"])
    if provider is None:
        raise SecretResolutionError(
            f"unsupported secret provider {match['provider']!r} in {value!r}; "
            f"available: {', '.join(sorted(_PROVIDERS))}"
        )
    try:
        secret = provider(match["target"])
    except SecretResolutionError as exc:
        raise SecretResolutionError(f"{value}: {exc}") from None
    if not secret:
        raise SecretResolutionError(f"{value}: resolved to an empty value")
    register_secret_value(secret)
    return secret


def _validate(value: Any) -> Any:
    if isinstance(value, SecretStr):
        register_secret_value(value.get_secret_value())
        return value
    if isinstance(value, str):
        return resolve_secret(value)
    return value


SecretValue = Annotated[SecretStr, BeforeValidator(_validate)]
"""A `SecretStr` field that accepts a literal or a `secret://` reference."""

"""Structured logging: JSON records with trace correlation and secret redaction.

`configure_logging` installs one handler on the root logger. Every record passes through
`RedactionFilter`, which masks registered secret values found in messages, exception text and
extra fields, and masks the value of any field whose name looks sensitive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import IO, Any

from opentelemetry import trace
from pydantic import SecretStr

from lorebase_platform.secrets import registered_secret_values
from lorebase_platform.settings import LoggingSettings

REDACTED = "***"
HANDLER_MARK = "_lorebase_handler"

request_id_var: ContextVar[str | None] = ContextVar("lorebase_request_id", default=None)

_STANDARD_ATTRIBUTES = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


@contextlib.contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """Bind a request id to every log record emitted inside the block."""
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


def is_sensitive_key(key: str, sensitive: tuple[str, ...]) -> bool:
    """True when `key` equals a sensitive name or contains it as underscore-separated parts."""
    normalized = f"_{key.lower().replace('-', '_')}_"
    return any(f"_{name.lower()}_" in normalized for name in sensitive)


class RedactionFilter(logging.Filter):
    """Mask secrets in log records before any formatter sees them."""

    def __init__(self, sensitive_keys: tuple[str, ...], min_length: int) -> None:
        super().__init__()
        self._keys = sensitive_keys
        self._min_length = min_length

    def scrub_text(self, text: str) -> str:
        """Replace every registered secret value found in `text`."""
        for secret in sorted(registered_secret_values(), key=len, reverse=True):
            if len(secret) >= self._min_length and secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def scrub(self, value: Any, key: str | None = None) -> Any:
        """Redact a value; a sensitive key masks the whole value."""
        if key is not None and is_sensitive_key(key, self._keys):
            return REDACTED
        if isinstance(value, SecretStr):
            return REDACTED
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            return {str(k): self.scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.scrub(item) for item in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub_text(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub_text(record.stack_info)
        for name in set(record.__dict__) - _STANDARD_ATTRIBUTES:
            record.__dict__[name] = self.scrub(record.__dict__[name], name)
        return True


def _trace_ids() -> dict[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line with trace and request correlation."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            **_trace_ids(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for name, value in record.__dict__.items():
            if name not in _STANDARD_ATTRIBUTES:
                payload[name] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable format that keeps the correlation identifiers."""

    def __init__(self, service: str) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ids = {**_trace_ids(), **({"request_id": r} if (r := request_id_var.get()) else {})}
        suffix = " ".join(f"{k}={v}" for k, v in ids.items())
        return f"{line} [{self._service}{' ' + suffix if suffix else ''}]"


def configure_logging(
    settings: LoggingSettings, *, service: str, stream: IO[str] | None = None
) -> logging.Handler:
    """Install the structured handler on the root logger (idempotent) and return it."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, HANDLER_MARK, False)]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    setattr(handler, HANDLER_MARK, True)
    handler.addFilter(RedactionFilter(settings.redact_keys, settings.redact_min_length))
    handler.setFormatter(
        JsonFormatter(service) if settings.format == "json" else TextFormatter(service)
    )
    root.addHandler(handler)
    root.setLevel(settings.level)
    return handler

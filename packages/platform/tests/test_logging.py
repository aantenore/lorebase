from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

from lorebase_platform.logging import (
    REDACTED,
    configure_logging,
    is_sensitive_key,
    request_context,
)
from lorebase_platform.secrets import register_secret_value
from lorebase_platform.settings import LoggingSettings

SECRET = "hunter2-very-secret"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    buffer = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG"), service="test-service", stream=buffer)
    yield buffer
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_emits_one_json_object_per_record(stream: io.StringIO) -> None:
    logging.getLogger("demo").info("hello %s", "world", extra={"order": 3})
    (record,) = records(stream)
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "demo"
    assert record["service"] == "test-service"
    assert record["order"] == 3
    assert "trace_id" not in record


def test_registered_secret_is_masked_in_message_args_and_extra(stream: io.StringIO) -> None:
    register_secret_value(SECRET)
    log = logging.getLogger("demo")
    log.warning("token is %s", SECRET)
    log.warning(f"inline {SECRET}")
    log.warning("extra", extra={"detail": f"value={SECRET}", "nested": {"a": [SECRET]}})
    output = stream.getvalue()
    assert SECRET not in output
    assert output.count(REDACTED) >= 4


def test_secret_in_exception_text_is_masked(stream: io.StringIO) -> None:
    register_secret_value(SECRET)
    try:
        raise RuntimeError(f"connection failed for {SECRET}")
    except RuntimeError:
        logging.getLogger("demo").exception("boom")
    (record,) = records(stream)
    assert SECRET not in json.dumps(record)
    assert "connection failed for ***" in record["exception"]


def test_sensitive_field_names_are_masked_even_for_unknown_values(stream: io.StringIO) -> None:
    logging.getLogger("demo").info(
        "login",
        extra={"password": "p4ss", "access_token": "abc", "input_tokens": 12, "user": "ann"},
    )
    (record,) = records(stream)
    assert record["password"] == REDACTED
    assert record["access_token"] == REDACTED
    assert record["input_tokens"] == 12
    assert record["user"] == "ann"


def test_secret_str_objects_are_masked(stream: io.StringIO) -> None:
    logging.getLogger("demo").info("cfg", extra={"key": SecretStr(SECRET)})
    assert SECRET not in stream.getvalue()


def test_short_secret_values_are_not_masked_in_free_text(stream: io.StringIO) -> None:
    register_secret_value("ab")
    logging.getLogger("demo").info("a cabinet")
    assert records(stream)[0]["message"] == "a cabinet"


def test_trace_and_request_ids_are_attached(stream: io.StringIO) -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("op") as span, request_context("req-1"):
        logging.getLogger("demo").info("inside")
        context = span.get_span_context()
    (record,) = records(stream)
    assert record["trace_id"] == f"{context.trace_id:032x}"
    assert record["span_id"] == f"{context.span_id:016x}"
    assert record["request_id"] == "req-1"
    logging.getLogger("demo").info("outside")
    assert "request_id" not in records(stream)[1]


def test_configure_logging_is_idempotent(stream: io.StringIO) -> None:
    second = io.StringIO()
    configure_logging(LoggingSettings(), service="again", stream=second)
    logging.getLogger("demo").warning("once")
    assert stream.getvalue() == ""
    assert len(second.getvalue().splitlines()) == 1


def test_text_format_keeps_request_id() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    buffer = io.StringIO()
    try:
        configure_logging(LoggingSettings(format="text"), service="svc", stream=buffer)
        with request_context("req-9"):
            logging.getLogger("demo").warning("plain")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert "plain" in buffer.getvalue()
    assert "request_id=req-9" in buffer.getvalue()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("password", True),
        ("api_key", True),
        ("X-Api-Key", True),
        ("access_token", True),
        ("input_tokens", False),
        ("monkey", False),
        ("secretary", False),
    ],
)
def test_sensitive_key_matching(key: str, expected: bool) -> None:
    keys = ("password", "secret", "token", "api_key")
    assert is_sensitive_key(key, keys) is expected

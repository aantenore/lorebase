"""Guide's agent trace defaults match Keeper's API (docs/openapi.json) and settings
(config/lorebase.example.yaml), so notes and headers reach Keeper without configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from lorebase_guide.settings import TraceSettings

ROOT = Path(__file__).resolve().parents[3]
API_PREFIX = "/api/v1"


def keeper_trace() -> dict[str, Any]:
    example = yaml.safe_load((ROOT / "config" / "lorebase.example.yaml").read_text())
    trace: dict[str, Any] = example["keeper"]["trace"]
    return trace


def test_default_notes_path_is_a_keeper_route() -> None:
    document = json.loads((ROOT / "docs" / "openapi.json").read_text())
    path = API_PREFIX + TraceSettings().notes_path.replace("{session}", "{session_id}")
    assert "post" in document["paths"][path]
    body = document["paths"][path]["post"]["requestBody"]["content"]["application/json"]
    schema = document["components"]["schemas"][body["schema"]["$ref"].rsplit("/", 1)[1]]
    assert {"text", "domain_id"} <= set(schema["properties"])


def test_default_headers_are_the_ones_keeper_reads() -> None:
    guide, keeper = TraceSettings(), keeper_trace()
    assert guide.session_header == keeper["session_header"]
    assert guide.tool_header == keeper["tool_header"]
    assert guide.opt_in_header == keeper["opt_in_header"]
    assert guide.max_note_chars <= keeper["max_note_chars"]

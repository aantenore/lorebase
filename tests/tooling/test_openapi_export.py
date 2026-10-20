"""docs/openapi.json is the OpenAPI document of the current code."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load()


def test_committed_document_matches_the_application() -> None:
    assert script.TARGET.read_text() == script.render(), "run `make openapi` and commit the result"


def test_check_flag_reports_a_stale_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n")
    monkeypatch.setattr(script, "TARGET", stale)
    assert script.main(["--check"]) == 1
    assert script.main([]) == 0
    assert script.main(["--check"]) == 0
    document = json.loads(stale.read_text())
    assert document["openapi"].startswith("3.")
    assert "/api/v1/domains" in document["paths"]

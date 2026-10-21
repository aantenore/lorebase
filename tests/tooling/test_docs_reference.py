"""The generated reference pages of the documentation site are the output of their sources."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "docs"


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # generate.py imports its sibling modules by name
    try:
        spec = importlib.util.spec_from_file_location(f"docs_{name}_script", SCRIPTS / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


generate = _load("generate")
config_reference = _load("config_reference")


def _leaves(node: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Paths of the values of a loaded document; an empty mapping counts as a value."""
    if isinstance(node, dict) and node:
        return {leaf for key, child in node.items() for leaf in _leaves(child, (*prefix, str(key)))}
    return {prefix}


def test_committed_pages_match_their_sources() -> None:
    assert generate.main(["--check"]) == 0, "run `make docs-generate` and commit the result"


def test_check_flag_reports_a_stale_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "page.md"
    stale.write_text("old\n")
    monkeypatch.setattr(generate, "PAGES", {"page": (stale, lambda: "new\n")})
    assert generate.main(["--check"]) == 1
    assert generate.main([]) == 0
    assert generate.main(["--check"]) == 0
    assert stale.read_text() == "new\n"


def test_configuration_page_lists_every_setting_of_the_example() -> None:
    text = config_reference.SOURCE.read_text()
    _, items = config_reference.parse(text)
    parsed = {
        item.path
        for item in items
        if isinstance(item, config_reference.Setting) and item.value is not None
    }
    leaves = _leaves(yaml.safe_load(text))
    # an inline mapping such as `retry: {max_retries: 3}` is one setting of the page
    assert {leaf for leaf in leaves if not any(leaf[: len(p)] == p for p in parsed)} == set()
    assert {p for p in parsed if not any(leaf[: len(p)] == p for leaf in leaves)} == set()
    page = config_reference.TARGET.read_text()
    assert all(f"`{'.'.join(path)}`" in page for path in parsed)

"""scripts/release_tool.py: the versions of the repository agree and the changelog is readable."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_tool.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_tool_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


def test_every_version_of_the_repository_agrees() -> None:
    assert tool.check(None) == []


def test_a_tag_other_than_the_versions_is_reported_for_every_file() -> None:
    problems = tool.check("v9.9.9")
    assert len(problems) == len(tool.versions())
    assert all("expected 9.9.9" in p for p in problems)


@pytest.mark.parametrize("tag", ["0.1.0", "v1.2", "v1.2.3-rc1", "vx.y.z"])
def test_a_malformed_tag_is_rejected(tag: str) -> None:
    [problem] = tool.check(tag)
    assert "is not vMAJOR.MINOR.PATCH" in problem


def test_pre_release_versions_have_a_python_spelling() -> None:
    assert tool.python_version("0.2.0-rc.1") == "0.2.0rc1"
    assert tool.python_version("1.0.0-beta.3") == "1.0.0b3"
    assert tool.python_version("0.1.0") == "0.1.0"


def test_the_changelog_has_a_section_for_the_current_version() -> None:
    version = tool.versions()["pyproject.toml"]
    body = tool.notes(version)
    assert len(body.splitlines()) > 3
    assert "## " not in body.replace("### ", "")  # it stops at the next version


def test_notes_stop_at_the_next_version_and_missing_versions_fail() -> None:
    text = "# Changelog\n\n## 0.2.0\n\nNew.\n\n### Added\n\n- b\n\n## 0.1.0\n\nOld.\n"
    assert tool.notes("0.2.0", text) == "New.\n\n### Added\n\n- b\n"
    assert tool.notes("0.1.0", text) == "Old.\n"
    with pytest.raises(SystemExit):
        tool.notes("0.3.0", text)

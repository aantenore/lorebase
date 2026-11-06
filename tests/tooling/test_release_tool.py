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


def test_a_tag_other_than_the_versions_is_reported_for_every_file_and_range() -> None:
    problems = tool.check("v9.9.9")
    assert len(problems) == len(tool.versions()) + len(tool.internal_ranges())
    assert sum("expected 9.9.9" in p for p in problems) == len(tool.versions())
    assert sum("expected >=9.9,<9.10" in p for p in problems) == len(tool.internal_ranges())


def test_internal_ranges_stop_before_the_next_minor(tmp_path: Path) -> None:
    package = tmp_path / "packages" / "keeper"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        '[project]\nname = "lorebase-keeper"\nversion = "0.2.0rc1"\n'
        'dependencies = ["lorebase-store>=0.2.0rc1,<0.3", "lorebase-core", "fastapi>=0.1"]\n'
    )
    assert tool.internal_ranges(tmp_path) == {
        "packages/keeper/pyproject.toml lorebase-store": ">=0.2.0rc1,<0.3",
        "packages/keeper/pyproject.toml lorebase-core": "",
    }
    assert tool.range_problems("0.2.0-rc.1", tmp_path) == [
        "packages/keeper/pyproject.toml lorebase-core: (no range), expected >=0.2,<0.3"
    ]


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


def test_every_package_has_its_pypi_metadata_and_license_copy() -> None:
    assert tool.metadata_problems() == []


def test_missing_pypi_metadata_and_a_stale_license_copy_are_reported(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("Apache License 2.0\n")
    package = tmp_path / "packages" / "herald"
    package.mkdir(parents=True)
    (package / "LICENSE").write_text("an older text\n")
    (package / "README.md").write_text("# lorebase-herald\n")
    (package / "pyproject.toml").write_text(
        '[project]\nname = "lorebase-herald"\nversion = "0.3.0"\nreadme = "README.md"\n'
        'license = "MIT"\nkeywords = ["mcp"]\n\n[project.urls]\nHomepage = "https://example.org"\n\n'
        '[build-system]\nrequires = ["hatchling"]\n'
    )
    problems = tool.metadata_problems(tmp_path)
    name = "packages/herald/pyproject.toml"
    assert f"{name}: license is not 'Apache-2.0'" in problems
    assert f"{name}: license-files is not ['LICENSE']" in problems
    assert "packages/herald/LICENSE: missing or not the repository LICENSE" in problems
    assert f"{name}: no authors" in problems
    assert f"{name}: no classifiers" in problems
    assert f"{name}: no keywords" not in problems
    assert f"{name}: no project URL Documentation" in problems
    assert f"{name}: no project URL Homepage" not in problems
    assert any("has no line 'mcp-name: io.github.aantenore/lorebase-herald'" in p for p in problems)
    assert f"{name}: build-system requires is not 'hatchling>=1.27'" in problems


def test_the_registry_entry_is_publishable() -> None:
    assert tool.registry_problems() == []


def test_a_registry_entry_the_registry_would_refuse_is_reported(tmp_path: Path) -> None:
    assert tool.registry_problems(tmp_path) == ["server.json: missing"]
    (tmp_path / "server.json").write_text(
        '{"name": "io.github.someone/other", "version": "0.3.0"}\n'
    )
    assert tool.registry_problems(tmp_path) == [
        "server.json: name is not 'io.github.aantenore/lorebase-herald'",
        "deploy/docker/backend.Dockerfile: no label "
        'io.modelcontextprotocol.server.name="io.github.aantenore/lorebase-herald"',
    ]

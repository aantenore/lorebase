"""Unit tests for scripts/test_changed.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "test_changed.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_changed_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tc = _load()

FILES = {
    "packages/core/src/lorebase_core/__init__.py": "",
    "packages/core/src/lorebase_core/canonical/__init__.py": "",
    "packages/core/src/lorebase_core/canonical/jcs.py": "",
    "packages/core/src/lorebase_core/tree/hamt.py": "",
    "packages/core/tests/canonical/test_jcs.py": "from lorebase_core.canonical import jcs\n",
    "packages/core/tests/canonical/test_hashing.py": "import lorebase_core.canonical\n",
    "packages/core/tests/canonical/conftest.py": "",
    "packages/core/tests/canonical/vectors/a.json": "{}",
    "packages/core/tests/tree/test_nodes.py": "from lorebase_core.tree.hamt import Hamt\n",
    "packages/core/tests/test_version.py": "import lorebase_core\n",
    "packages/store/tests/test_repo.py": "from lorebase_core.tree import hamt\n",
    "packages/store/tests/test_other.py": "import json\n",
    "packages/platform/src/lorebase_platform/settings.py": "",
    "packages/platform/tests/test_settings.py": "def test_x(): ...\n",
    "packages/platform/pyproject.toml": "",
    "scripts/tool.py": "",
    "tests/tooling/test_tool.py": "SCRIPT = 'scripts/tool.py'\n",
    ".importlinter": "",
    "tests/tooling/test_contracts.py": "CONFIG = '.importlinter'\n",
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name, content in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def select(tree: Path, *changed: str) -> list[str]:
    return tc.select_tests(changed, tree)


def test_changed_test_file_selects_itself(tree: Path) -> None:
    changed = "packages/platform/tests/test_settings.py"
    assert select(tree, changed) == [changed]


def test_module_selects_tests_importing_it_directly(tree: Path) -> None:
    assert select(tree, "packages/core/src/lorebase_core/canonical/jcs.py") == [
        "packages/core/tests/canonical/test_hashing.py",
        "packages/core/tests/canonical/test_jcs.py",
    ]


def test_module_selects_tests_in_other_packages_importing_it(tree: Path) -> None:
    assert select(tree, "packages/core/src/lorebase_core/tree/hamt.py") == [
        "packages/core/tests/tree/test_nodes.py",
        "packages/store/tests/test_repo.py",
    ]


def test_module_selects_test_named_after_it(tree: Path) -> None:
    assert select(tree, "packages/platform/src/lorebase_platform/settings.py") == [
        "packages/platform/tests/test_settings.py"
    ]


def test_package_init_selects_importers_of_the_package(tree: Path) -> None:
    selected = select(tree, "packages/core/src/lorebase_core/canonical/__init__.py")
    assert "packages/core/tests/canonical/test_jcs.py" in selected
    assert "packages/core/tests/canonical/test_hashing.py" in selected
    assert "packages/core/tests/tree/test_nodes.py" not in selected


def test_conftest_selects_tests_of_its_directory_only(tree: Path) -> None:
    assert select(tree, "packages/core/tests/canonical/conftest.py") == [
        "packages/core/tests/canonical/test_hashing.py",
        "packages/core/tests/canonical/test_jcs.py",
    ]


def test_data_file_selects_tests_of_the_nearest_test_directory(tree: Path) -> None:
    assert select(tree, "packages/core/tests/canonical/vectors/a.json") == [
        "packages/core/tests/canonical/test_hashing.py",
        "packages/core/tests/canonical/test_jcs.py",
    ]


def test_package_pyproject_selects_all_tests_of_the_package(tree: Path) -> None:
    assert select(tree, "packages/platform/pyproject.toml") == [
        "packages/platform/tests/test_settings.py"
    ]


def test_non_package_file_selects_tests_mentioning_it(tree: Path) -> None:
    assert select(tree, "scripts/tool.py") == ["tests/tooling/test_tool.py"]
    assert select(tree, ".importlinter") == ["tests/tooling/test_contracts.py"]


def test_unrelated_change_selects_nothing(tree: Path) -> None:
    assert select(tree, "docs/design.md", "README.md") == []


def test_module_of_maps_source_paths() -> None:
    module_of = tc.module_of
    assert module_of(PurePosixPath("packages/core/src/lorebase_core/canonical/jcs.py")) == (
        "lorebase_core.canonical.jcs"
    )
    assert module_of(PurePosixPath("packages/core/src/lorebase_core/canonical/__init__.py")) == (
        "lorebase_core.canonical"
    )
    assert module_of(PurePosixPath("packages/core/tests/test_x.py")) is None
    assert module_of(PurePosixPath("scripts/tool.py")) is None


def test_imported_modules_collects_absolute_imports() -> None:
    source = "import a.b\nfrom c import d\nfrom . import e\nfrom f.g import (h, i)\n"
    assert tc.imported_modules(source) == {"a.b", "c", "c.d", "f.g", "f.g.h", "f.g.i"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_changed_files_lists_committed_unstaged_and_untracked(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "base.py").write_text("1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "work")
    (tmp_path / "committed.py").write_text("1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "work")
    (tmp_path / "base.py").write_text("2\n")
    (tmp_path / "untracked.py").write_text("1\n")

    base = tc.resolve_base(tmp_path, None)
    assert base == "main"
    assert tc.changed_files(tmp_path, base) == ["base.py", "committed.py", "untracked.py"]

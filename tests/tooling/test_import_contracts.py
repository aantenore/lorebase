"""The import-linter contracts hold on the repository and fail on forbidden imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".importlinter"
LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
ROOT_PACKAGES = [
    "lorebase_core",
    "lorebase_platform",
    "lorebase_inference",
    "lorebase_store",
    "lorebase_keeper",
    "lorebase_client",
    "lorebase_cli",
    "lorebase_herald",
    "lorebase_scribe",
    "lorebase_guide",
]


def _run(cwd: Path, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [str(LINT_IMPORTS), "--config", str(CONFIG)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_workspace(base: Path, violation: tuple[str, str]) -> None:
    """Create empty root packages; `violation` is (importing package, imported package)."""
    for name in ROOT_PACKAGES:
        package = base / name
        package.mkdir()
        (package / "__init__.py").write_text("")
    importer, imported = violation
    (base / importer / "uses.py").write_text(f"import {imported}\n")


def test_repository_respects_contracts() -> None:
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "0 broken" in result.stdout


@pytest.mark.parametrize(
    ("violation", "contract"),
    [
        (("lorebase_core", "lorebase_store"), "core has no internal dependencies"),
        (
            ("lorebase_keeper", "lorebase_inference"),
            "keeper depends on store and never on inference",
        ),
        (("lorebase_store", "lorebase_keeper"), "store depends on core and platform only"),
        (("lorebase_inference", "lorebase_store"), "inference depends on core and platform only"),
        (("lorebase_cli", "lorebase_store"), "cli depends on client only"),
        (("lorebase_platform", "lorebase_core"), "platform has no internal dependencies"),
        (
            ("lorebase_herald", "lorebase_inference"),
            "herald depends on platform and client only and never on inference",
        ),
        (
            ("lorebase_herald", "lorebase_store"),
            "herald depends on platform and client only and never on inference",
        ),
        (
            ("lorebase_scribe", "lorebase_keeper"),
            "scribe depends on store, platform, inference and core, never on keeper",
        ),
        (("lorebase_store", "lorebase_scribe"), "store depends on core and platform only"),
        (
            ("lorebase_keeper", "lorebase_scribe"),
            "keeper depends on store and never on inference",
        ),
        (("lorebase_keeper", "lorebase_guide"), "keeper depends on store and never on inference"),
        (
            ("lorebase_herald", "lorebase_guide"),
            "herald depends on platform and client only and never on inference",
        ),
        (("lorebase_store", "lorebase_guide"), "store depends on core and platform only"),
        (("lorebase_core", "lorebase_guide"), "core has no internal dependencies"),
        (
            ("lorebase_guide", "lorebase_keeper"),
            "guide depends on platform, inference and client only",
        ),
        (
            ("lorebase_guide", "lorebase_store"),
            "guide depends on platform, inference and client only",
        ),
    ],
)
def test_forbidden_import_breaks_contract(
    tmp_path: Path, violation: tuple[str, str], contract: str
) -> None:
    _make_workspace(tmp_path, violation)
    result = _run(tmp_path, pythonpath=tmp_path)
    assert result.returncode != 0
    assert f"{contract} BROKEN" in result.stdout

#!/usr/bin/env python3
"""Run only the tests impacted by the current diff.

Changed files are taken from `git diff` against the merge base with the base branch, plus
untracked files. A test file is selected when it:

- changed itself;
- imports a changed source module (or a package that contains it, below the root package);
- is named `test_<module>.py` for a changed source module of the same package;
- mentions the name of a changed file outside package sources (scripts, config, dotfiles);
- lives in a directory affected by a changed conftest or test data file;
- belongs to a package whose `pyproject.toml` changed.

Imports are followed one level only: the selection is a fast local approximation, and CI runs
the full suite.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

BASE_REF_ENV = "LOREBASE_BASE_REF"
BASE_REF_CANDIDATES = ("origin/main", "main")
PACKAGES_DIR = "packages"
SOURCE_DIR = "src"
TESTS_DIR = "tests"
TEST_PREFIX = "test_"
TEST_SUFFIX = "_test.py"
SKIPPED_DIRS = frozenset({".venv", "node_modules", ".git", "__pycache__"})


def is_test_file(path: PurePosixPath) -> bool:
    """True for a Python test module living under a `tests` directory."""
    return (
        TESTS_DIR in path.parts
        and path.suffix == ".py"
        and (path.name.startswith(TEST_PREFIX) or path.name.endswith(TEST_SUFFIX))
    )


def module_of(path: PurePosixPath) -> str | None:
    """Dotted module name of `packages/<pkg>/src/<module>/...py`, or None."""
    parts = path.parts
    if len(parts) < 4 or parts[0] != PACKAGES_DIR or parts[2] != SOURCE_DIR:
        return None
    if path.suffix != ".py":
        return None
    dotted = list(parts[3:-1])
    if path.stem != "__init__":
        dotted.append(path.stem)
    return ".".join(dotted) or None


def package_of(path: PurePosixPath) -> str | None:
    """Workspace package directory name (`core`, `store`, ...) a path belongs to."""
    parts = path.parts
    return parts[1] if len(parts) > 2 and parts[0] == PACKAGES_DIR else None


def imported_modules(source: str) -> set[str]:
    """Absolute module names imported by a source text (relative imports are ignored)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _imports_module(imports: Iterable[str], module: str) -> bool:
    """True when any import is the module, one of its parents (below root) or a child."""
    for name in imports:
        if name == module or name.startswith(module + "."):
            return True
        if "." in name and module.startswith(name + "."):
            return True
    return False


def discover_tests(root: Path) -> list[PurePosixPath]:
    """All test files of the workspace, relative to `root`, sorted."""
    found: list[PurePosixPath] = []
    for base in (root / TESTS_DIR, *sorted((root / PACKAGES_DIR).glob(f"*/{TESTS_DIR}"))):
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if not SKIPPED_DIRS.intersection(rel.parts) and is_test_file(rel):
                found.append(rel)
    return sorted(found)


def _tests_under(tests: Sequence[PurePosixPath], directory: PurePosixPath) -> set[PurePosixPath]:
    return {t for t in tests if directory in t.parents or t.parent == directory}


def _support_scope(path: PurePosixPath, tests: Sequence[PurePosixPath]) -> set[PurePosixPath]:
    """Tests affected by a conftest or data file: the nearest ancestor directory with tests."""
    for directory in path.parents:
        scoped = _tests_under(tests, directory)
        if scoped:
            return scoped
        if directory.name == TESTS_DIR:
            break
    return set()


def select_tests(
    changed: Iterable[str],
    root: Path,
    tests: Sequence[PurePosixPath] | None = None,
) -> list[str]:
    """Map changed files (posix, relative to `root`) to impacted test files."""
    test_files = list(tests) if tests is not None else discover_tests(root)
    selected: set[PurePosixPath] = set()
    imports_cache: dict[PurePosixPath, set[str]] = {}
    text_cache: dict[PurePosixPath, str] = {}

    def text_of(test: PurePosixPath) -> str:
        if test not in text_cache:
            text_cache[test] = (root / test).read_text(encoding="utf-8", errors="replace")
        return text_cache[test]

    def imports_of(test: PurePosixPath) -> set[str]:
        if test not in imports_cache:
            imports_cache[test] = imported_modules(text_of(test))
        return imports_cache[test]

    for name in changed:
        path = PurePosixPath(name)
        if is_test_file(path):
            if path in test_files:
                selected.add(path)
            continue
        module = module_of(path)
        if module is not None:
            package = package_of(path)
            stem = module.rsplit(".", 1)[-1]
            for test in test_files:
                same_package = package_of(test) == package
                if (same_package and test.name == f"{TEST_PREFIX}{stem}.py") or _imports_module(
                    imports_of(test), module
                ):
                    selected.add(test)
        elif TESTS_DIR in path.parts:
            selected |= _support_scope(path, test_files)
        elif path.name == "pyproject.toml" and package_of(path) is not None:
            selected |= _tests_under(test_files, PurePosixPath(PACKAGES_DIR, path.parts[1]))
        elif PACKAGES_DIR not in path.parts:
            selected |= {t for t in test_files if path.name in text_of(t)}
    return sorted(str(t) for t in selected)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_base(root: Path, requested: str | None) -> str:
    """Pick the base ref: explicit argument or env, then origin/main, then main."""
    for candidate in (requested, os.environ.get(BASE_REF_ENV), *BASE_REF_CANDIDATES):
        if candidate and _git(root, "rev-parse", "--verify", "--quiet", candidate):
            return candidate
    return "HEAD"


def changed_files(root: Path, base: str) -> list[str]:
    """Files changed since the merge base (committed, staged, unstaged) plus untracked."""
    merge_base = _git(root, "merge-base", "HEAD", base) or "HEAD"
    tracked = _git(root, "diff", "--name-only", "--diff-filter=ACMR", merge_base).splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({*tracked, *untracked})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--base", help=f"base ref (default: ${BASE_REF_ENV}, origin/main, main)")
    parser.add_argument("--list", action="store_true", help="print selected tests and exit")
    parser.add_argument("pytest_args", nargs="*", help="extra arguments passed to pytest")
    args = parser.parse_args(argv)

    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel") or Path.cwd())
    changed = changed_files(root, resolve_base(root, args.base))
    selected = select_tests(changed, root)
    if not selected:
        print("test-changed: no impacted tests for the current diff")
        return 0
    if args.list:
        print("\n".join(selected))
        return 0
    command = [sys.executable, "-m", "pytest", *selected, *args.pytest_args]
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())

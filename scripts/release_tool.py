#!/usr/bin/env python3
"""Checks for a release (docs/release.md).

    release_tool.py check [--tag v0.1.0]   every version in the repository is the same, and
                                           equal to the tag when one is given
    release_tool.py notes 0.1.0            print the CHANGELOG.md section of a version

The versions that must agree are the root and package `pyproject.toml` files, the
`__version__` of each Python package, `apps/web/package.json` and `appVersion` of the Helm chart.
A pre-release tag such as `v0.2.0-rc.1` is the npm and Helm version `0.2.0-rc.1` and the Python
version `0.2.0rc1`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-(?:alpha|beta|rc)\.\d+)?$")
PREVIEW_SUFFIX = re.compile(r"-(alpha|beta|rc)\.(\d+)$")
PYTHON_SUFFIX = {"alpha": "a", "beta": "b", "rc": "rc"}
VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def python_version(version: str) -> str:
    """The PEP 440 form of a semver version (`0.2.0-rc.1` is `0.2.0rc1`)."""
    return PREVIEW_SUFFIX.sub(lambda m: PYTHON_SUFFIX[m.group(1)] + m.group(2), version)


def versions(root: Path = ROOT) -> dict[str, str]:
    """Every version found in the repository, by file."""
    found: dict[str, str] = {}
    projects = [root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml"))]
    for path in projects:
        found[str(path.relative_to(root))] = tomllib.loads(path.read_text("utf-8"))["project"][
            "version"
        ]
    for path in sorted((root / "packages").glob("*/src/*/__init__.py")):
        match = VERSION_ASSIGNMENT.search(path.read_text("utf-8"))
        found[str(path.relative_to(root))] = match.group(1) if match else "(no __version__)"
    web = root / "apps" / "web" / "package.json"
    found[str(web.relative_to(root))] = json.loads(web.read_text("utf-8"))["version"]
    chart = root / "deploy" / "helm" / "lorebase" / "Chart.yaml"
    found[f"{chart.relative_to(root)} (appVersion)"] = str(
        yaml.safe_load(chart.read_text("utf-8"))["appVersion"]
    )
    return found


def check(tag: str | None, root: Path = ROOT) -> list[str]:
    """Problems found: files whose version differs from the expected one."""
    found = versions(root)
    if tag is None:
        expected = found["pyproject.toml"]
    else:
        if not tag.startswith("v") or not SEMVER.match(tag[1:]):
            return [f"tag {tag!r} is not vMAJOR.MINOR.PATCH (optionally -alpha.N, -beta.N, -rc.N)"]
        expected = tag[1:]
    problems = []
    for name, version in found.items():
        wanted = python_version(expected) if name.endswith((".toml", "__init__.py")) else expected
        if version != wanted:
            problems.append(f"{name}: {version}, expected {wanted}")
    return problems


def notes(version: str, text: str | None = None) -> str:
    """The body of the `## <version>` section of the changelog, without its heading."""
    text = CHANGELOG.read_text("utf-8") if text is None else text
    heading = re.compile(rf"^## \[?{re.escape(version)}\]?(?:\s.*)?$", re.MULTILINE)
    start = heading.search(text)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    following = re.search(r"^## ", text[start.end() :], re.MULTILINE)
    end = start.end() + following.start() if following else len(text)
    body = text[start.end() : end].strip()
    if not body:
        raise SystemExit(f"the section for {version} in CHANGELOG.md is empty")
    return body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="all versions agree")
    check_parser.add_argument("--tag", help="the release tag, for example v0.1.0")
    notes_parser = sub.add_parser("notes", help="print a changelog section")
    notes_parser.add_argument("version", help="for example 0.1.0")
    args = parser.parse_args(argv)
    if args.command == "notes":
        sys.stdout.write(notes(args.version))
        return 0
    problems = check(args.tag)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"all {len(versions())} versions agree")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

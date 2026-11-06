#!/usr/bin/env python3
"""Checks for a release (docs/release.md).

    release_tool.py check [--tag v0.1.0]   every version in the repository is the same, and
                                           equal to the tag when one is given
    release_tool.py notes 0.1.0            print the CHANGELOG.md section of a version

The versions that must agree are the root and package `pyproject.toml` files, the
`__version__` of each Python package, `apps/web/package.json`, `appVersion` of the Helm chart and
`server.json` (the MCP registry entry of Herald: its `version`, the version of its PyPI package
and the tag of its image).
The dependencies of one workspace package on another must admit the version and stop before the
next minor (`lorebase-core>=0.1,<0.2`; for a pre-release the lower bound may be the pre-release).
A pre-release tag such as `v0.2.0-rc.1` is the npm and Helm version `0.2.0-rc.1` and the Python
version `0.2.0rc1`. Every package also carries the metadata of its PyPI page (license, README,
authors, keywords, classifiers, project URLs) and its own copy of the repository LICENSE, so the
sdists and wheels ship the license text.
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
INTERNAL_REQUIREMENT = re.compile(r"^(lorebase-[a-z0-9-]+)(?:\[[^\]]*\])?\s*(.*)$")
# PyPI metadata every package declares; hatchling reads `license` and `license-files` (PEP 639)
# from 1.27 on.
LICENSE_EXPRESSION = "Apache-2.0"
LICENSE_FILE = "LICENSE"
REQUIRED_FIELDS = ("readme", "authors", "keywords", "classifiers")
REQUIRED_URLS = ("Homepage", "Documentation", "Repository", "Changelog", "Issues")
BUILD_BACKEND_REQUIREMENT = "hatchling>=1.27"
# The MCP registry entry (server.json) and how the registry proves ownership of what it names:
# a marker line in the PyPI description of the server package, a label on the image.
MCP_SERVER_NAME = "io.github.aantenore/lorebase-herald"
MCP_NAME_MARKERS = {"lorebase-herald": f"mcp-name: {MCP_SERVER_NAME}"}
REGISTRY_FILE = "server.json"
REGISTRY_IMAGE_LABEL = f'io.modelcontextprotocol.server.name="{MCP_SERVER_NAME}"'
REGISTRY_IMAGE_DOCKERFILE = Path("deploy") / "docker" / "backend.Dockerfile"


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
    registry = json.loads((root / REGISTRY_FILE).read_text("utf-8"))
    found[f"{REGISTRY_FILE} (version)"] = str(registry["version"])
    for package in registry.get("packages", []):
        identifier = str(package["identifier"])
        if package.get("registryType") == "oci":
            image, _, tag = identifier.rpartition(":")
            found[f"{REGISTRY_FILE} ({image} tag)"] = tag
        else:
            kind = package.get("registryType")
            found[f"{REGISTRY_FILE} ({kind} {identifier})"] = str(package["version"])
    return found


def python_versioned(name: str) -> bool:
    """Whether the file names the version in its PEP 440 form (`0.2.0rc1`, not `0.2.0-rc.1`)."""
    return name.endswith((".toml", "__init__.py")) or "(pypi " in name


def internal_ranges(root: Path = ROOT) -> dict[str, str]:
    """The version range of every dependency of a package on another workspace package."""
    found: dict[str, str] = {}
    for path in sorted((root / "packages").glob("*/pyproject.toml")):
        project = tomllib.loads(path.read_text("utf-8"))["project"]
        for requirement in project.get("dependencies", []):
            match = INTERNAL_REQUIREMENT.match(requirement.strip())
            if match:
                spec = match.group(2).replace(" ", "")
                found[f"{path.relative_to(root)} {match.group(1)}"] = spec
    return found


def range_problems(expected: str, root: Path = ROOT) -> list[str]:
    """Internal ranges other than `>=X.Y,<X.Y+1` (or `>=<pre-release>,<X.Y+1`)."""
    major, minor = (int(part) for part in expected.split(".")[:2])
    lower = {f"{major}.{minor}", python_version(expected)}
    upper = f"{major}.{minor + 1}"
    wanted = f">={major}.{minor},<{upper}"
    return [
        f"{name}: {spec or '(no range)'}, expected {wanted}"
        for name, spec in internal_ranges(root).items()
        if not any(spec == f">={bound},<{upper}" for bound in lower)
    ]


def metadata_problems(root: Path = ROOT) -> list[str]:
    """Packages whose PyPI metadata is incomplete, or whose LICENSE copy differs from the
    repository's."""
    problems: list[str] = []
    license_text = (root / LICENSE_FILE).read_bytes()
    for path in sorted((root / "packages").glob("*/pyproject.toml")):
        name = str(path.relative_to(root))
        document = tomllib.loads(path.read_text("utf-8"))
        project = document["project"]
        if project.get("license") != LICENSE_EXPRESSION:
            problems.append(f"{name}: license is not {LICENSE_EXPRESSION!r}")
        if project.get("license-files") != [LICENSE_FILE]:
            problems.append(f"{name}: license-files is not [{LICENSE_FILE!r}]")
        copy = path.parent / LICENSE_FILE
        if not copy.is_file() or copy.read_bytes() != license_text:
            problems.append(
                f"{copy.relative_to(root)}: missing or not the repository {LICENSE_FILE}"
            )
        problems += [f"{name}: no {field}" for field in REQUIRED_FIELDS if not project.get(field)]
        urls = project.get("urls", {})
        problems += [f"{name}: no project URL {key}" for key in REQUIRED_URLS if not urls.get(key)]
        readme = project.get("readme")
        if isinstance(readme, str):
            readme_path = path.parent / readme
            if not readme_path.is_file():
                problems.append(f"{name}: readme {readme} does not exist")
            else:
                marker = MCP_NAME_MARKERS.get(project["name"])
                if marker and marker not in readme_path.read_text("utf-8").splitlines():
                    problems.append(f"{name}: {readme} has no line {marker!r}")
        if BUILD_BACKEND_REQUIREMENT not in document.get("build-system", {}).get("requires", []):
            problems.append(f"{name}: build-system requires is not {BUILD_BACKEND_REQUIREMENT!r}")
    return problems


def registry_problems(root: Path = ROOT) -> list[str]:
    """server.json names Herald as the registry knows it, and the image it names carries the
    label the registry checks."""
    registry = root / REGISTRY_FILE
    if not registry.is_file():
        return [f"{REGISTRY_FILE}: missing"]
    problems: list[str] = []
    if json.loads(registry.read_text("utf-8")).get("name") != MCP_SERVER_NAME:
        problems.append(f"{REGISTRY_FILE}: name is not {MCP_SERVER_NAME!r}")
    dockerfile = root / REGISTRY_IMAGE_DOCKERFILE
    if not dockerfile.is_file() or REGISTRY_IMAGE_LABEL not in dockerfile.read_text("utf-8"):
        problems.append(f"{REGISTRY_IMAGE_DOCKERFILE}: no label {REGISTRY_IMAGE_LABEL}")
    return problems


def check(tag: str | None, root: Path = ROOT) -> list[str]:
    """Problems found: files whose version differs from the expected one, internal ranges that
    do not admit it, packages with incomplete PyPI metadata, and a registry entry the registry
    would refuse."""
    found = versions(root)
    if tag is None:
        expected = found["pyproject.toml"]
    else:
        if not tag.startswith("v") or not SEMVER.match(tag[1:]):
            return [f"tag {tag!r} is not vMAJOR.MINOR.PATCH (optionally -alpha.N, -beta.N, -rc.N)"]
        expected = tag[1:]
    problems = []
    for name, version in found.items():
        wanted = python_version(expected) if python_versioned(name) else expected
        if version != wanted:
            problems.append(f"{name}: {version}, expected {wanted}")
    return (
        problems
        + range_problems(expected, root)
        + metadata_problems(root)
        + registry_problems(root)
    )


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
        print(
            f"all {len(versions())} versions and {len(internal_ranges())} internal ranges agree,"
            " every package has its PyPI metadata and the registry entry is publishable"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Relative links and anchors of the Markdown pages outside the documentation site resolve.

`mkdocs build --strict` checks the pages under docs/; the root pages and the examples index are
read on GitHub, so their links are checked here: the target file exists and, when the link names
a heading, the target page has it.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "examples/README.md")

INLINE_LINK = re.compile(r"(?<=\])\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK = re.compile(r"\b(?:href|src)=[\"'](?P<target>[^\"']+)[\"']")
HTML_ID = re.compile(r"\b(?:id|name)=[\"'](?P<id>[^\"']+)[\"']")
CODE_SPAN = re.compile(r"`[^`]*`")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
HEADING = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*#*\s*$")


def _prose_lines(text: str) -> list[str]:
    """The lines outside fenced code blocks, with code spans removed."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            lines.append(CODE_SPAN.sub("", line))
    return lines


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading (the site uses the same, see mkdocs.yml)."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link keeps its text
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    text = path.read_text()
    for line in _prose_lines(text):
        found.update(match["id"] for match in HTML_ID.finditer(line))
        heading = HEADING.match(line)
        if heading is None:
            continue
        base = slug(heading["text"])
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return frozenset(found)


def links(page: Path) -> list[str]:
    targets: list[str] = []
    for line in _prose_lines(page.read_text()):
        targets.extend(match["target"] for match in INLINE_LINK.finditer(line))
        targets.extend(match["target"] for match in HTML_LINK.finditer(line))
    return targets


def problems(page: Path) -> list[str]:
    found: list[str] = []
    for target in links(page):
        if SCHEME.match(target) or target.startswith("/"):
            continue  # external, or absolute on the site
        path, _, fragment = target.partition("#")
        resolved = (page.parent / path).resolve() if path else page
        if not resolved.exists():
            found.append(f"{target}: no such file")
        elif fragment and resolved.suffix == ".md" and fragment not in anchors(resolved):
            found.append(f"{target}: no heading #{fragment} in {path or page.name}")
    return found


@pytest.mark.parametrize("name", PAGES)
def test_relative_links_and_anchors_resolve(name: str) -> None:
    page = ROOT / name
    assert links(page), f"{name} has no links: the parser missed them"
    assert problems(page) == []


def test_a_broken_anchor_and_a_missing_file_are_reported(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Other\n\n## 2.4 Load the example data\n")
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\n"
        "[ok](other.md#24-load-the-example-data) [self](#page) [web](https://example.com)\n"
        "[stale](other.md#11-import-the-sample-data) [gone](missing.md)\n"
        "```text\n[in a fence](nowhere.md)\n```\n"
        "`[in a code span](nowhere.md)`\n"
    )
    assert problems(page) == [
        "other.md#11-import-the-sample-data: no heading #11-import-the-sample-data in other.md",
        "missing.md: no such file",
    ]

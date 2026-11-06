"""examples/merge_demo.py runs end to end and tells the same story every time.

The pages that quote its output quote it as the script prints it today, Revision ids included.
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "examples" / "merge_demo.py"
# Pages with excerpts of the demo output, in ```text blocks that hold a `== N.` step heading.
QUOTING_PAGES = (
    "README.md",
    "docs/index.md",
    "docs/manual-setup.md",
    "docs/launch/blog-post.md",
)
STEP_HEADING = re.compile(r"^== \d+\. ")
ELISION = "..."


def run_demo() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@cache
def demo_lines() -> frozenset[str]:
    result = run_demo()
    assert result.returncode == 0, result.stderr
    return frozenset(line.rstrip() for line in result.stdout.splitlines())


def quoted_excerpts(text: str) -> list[list[str]]:
    """The ```text blocks of a page that quote the demo, as lists of lines."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if current is None:
            if line.strip() == "```text":
                current = []
        elif line.strip() == "```":
            if any(STEP_HEADING.match(quoted) for quoted in current):
                blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def test_merge_demo_runs_and_reports_the_story() -> None:
    first = run_demo()
    assert first.returncode == 0, first.stderr
    lines = first.stdout.splitlines()
    for expected in (
        "schema: 6 Kinds, 7 Relations, 0 problems",
        "fast_forward: 34 changes in 4 partitions",
        "DV1 ATTRIBUTE_VALUE (1)",
        "DV2 DELETE_MODIFY (1)",
        "DV3 ADD_ADD (2)",
        "XS4 MISSING_VALUE (1)",
        "validation: 72 Records against the merged schema, 0 issues",
        "S-1001 risk_score: 32 -> 75",
    ):
        assert expected in lines
    assert any(line.startswith("merged: Revision ") for line in lines)
    assert "5 Conflicts" in first.stdout
    assert run_demo().stdout == first.stdout  # deterministic


@pytest.mark.parametrize("page", QUOTING_PAGES)
def test_quoted_output_matches_the_demo(page: str) -> None:
    excerpts = quoted_excerpts((ROOT / page).read_text())
    assert excerpts, f"{page} quotes no demo output: update QUOTING_PAGES"
    printed = demo_lines()
    stale = [
        line
        for excerpt in excerpts
        for line in excerpt
        if line.strip() not in ("", ELISION) and line.rstrip() not in printed
    ]
    assert stale == [], f"{page}: rerun examples/merge_demo.py and paste its output"

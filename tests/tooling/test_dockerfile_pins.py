"""Every external base image of the Dockerfiles is pinned by the digest of its index."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[2] / "deploy" / "docker"
DOCKERFILES = sorted(DOCKER_DIR.glob("*.Dockerfile"))
PINNED = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
FROM = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG = re.compile(r"^ARG\s+(\w+)=(\S*)")


def _external_images(path: Path) -> list[str]:
    defaults: dict[str, str] = {}
    stages: set[str] = set()
    images: list[str] = []
    for line in path.read_text().splitlines():
        arg = ARG.match(line)
        if arg:
            defaults[arg.group(1)] = arg.group(2)
            continue
        match = FROM.match(line)
        if not match:
            continue
        reference = re.sub(
            r"\$\{(\w+)\}", lambda m: defaults.get(m.group(1), m.group(0)), match.group(1)
        )
        if reference not in stages and reference != "scratch":
            images.append(reference)
        if match.group(2):
            stages.add(match.group(2))
    return images


def test_dockerfiles_are_found() -> None:
    assert {path.name for path in DOCKERFILES} >= {"backend.Dockerfile", "web.Dockerfile"}


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda path: path.name)
def test_external_images_are_pinned_by_digest(path: Path) -> None:
    images = _external_images(path)
    assert images, f"{path.name} has no external FROM"
    unpinned = [image for image in images if not PINNED.match(image)]
    assert not unpinned, f"{path.name}: not pinned by @sha256:<64 hex>: {unpinned}"

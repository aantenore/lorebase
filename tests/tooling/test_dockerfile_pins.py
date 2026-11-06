"""Every external image is pinned by the digest of its index: the base images of the Dockerfiles,
the third-party defaults of the compose files and of the Helm chart."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = ROOT / "deploy" / "docker"
DOCKERFILES = sorted(DOCKER_DIR.glob("*.Dockerfile"))
COMPOSE_FILES = sorted((ROOT / "deploy" / "compose").glob("*.yaml"))
HELM_VALUES = ROOT / "deploy" / "helm" / "lorebase" / "values.yaml"
# Images of this repository, tagged by the release (or built from the checkout by compose).
OWN_IMAGES = re.compile(r"^(ghcr\.io/aantenore/)?lorebase(-[a-z]+)?$")
PINNED = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
# Third-party defaults name a version as well, so a bump shows what changed.
VERSION_AND_DIGEST = re.compile(r"^[^\s@:]+(:\d+)?(/[^\s@:]+)*:[^\s@:]+@sha256:[0-9a-f]{64}$")
# `image: ${VARIABLE:-default}` of a compose service; anything after the brace is a built image.
COMPOSE_IMAGE = re.compile(r"^\s*image:\s*\$\{(\w+):-([^}]*)\}(\S*)\s*$")
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


def _compose_defaults() -> list[tuple[str, str, str]]:
    found = []
    for path in COMPOSE_FILES:
        for line in path.read_text().splitlines():
            match = COMPOSE_IMAGE.match(line)
            if match and not match.group(3):
                found.append((path.name, match.group(1), match.group(2)))
    return found


def test_compose_images_are_found() -> None:
    variables = {variable for _, variable, _ in _compose_defaults()}
    assert {"POSTGRES_IMAGE", "OLLAMA_IMAGE", "GRAFANA_IMAGE", "PROXY_IMAGE"} <= variables


@pytest.mark.parametrize(
    ("name", "variable", "default"), _compose_defaults(), ids=lambda value: str(value)
)
def test_compose_defaults_are_pinned_by_version_and_digest(
    name: str, variable: str, default: str
) -> None:
    assert VERSION_AND_DIGEST.match(default), f"{name}: ${{{variable}}} defaults to {default}"


def test_a_compose_variable_has_one_default_everywhere() -> None:
    defaults: dict[str, set[str]] = {}
    for _, variable, default in _compose_defaults():
        defaults.setdefault(variable, set()).add(default)
    assert {k: v for k, v in defaults.items() if len(v) > 1} == {}


def _helm_images(node: object, path: str = "") -> list[tuple[str, str, str]]:
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("repository"), str):
            found.append((path, node["repository"], str(node.get("tag", ""))))
        for key, value in node.items():
            found += _helm_images(value, f"{path}.{key}" if path else str(key))
    return found


def test_helm_third_party_images_are_pinned_by_version_and_digest() -> None:
    images = _helm_images(yaml.safe_load(HELM_VALUES.read_text()))
    third_party = [
        (path, repository, tag)
        for path, repository, tag in images
        if repository and not OWN_IMAGES.match(repository)
    ]
    assert any(repository == "ollama/ollama" for _, repository, _ in third_party)
    for path, repository, tag in third_party:
        assert VERSION_AND_DIGEST.match(f"{repository}:{tag}"), f"{path}: {repository}:{tag}"

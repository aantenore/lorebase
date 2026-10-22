"""Scribe and Guide run on the embedding image (imageMl) in the Helm chart; Keeper and the
migration keep the backend image. An empty imageMl.repository puts all of them on one image."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "lorebase"
HELM = shutil.which("helm")
GUIDE = ["-f", str(CHART / "ci" / "guide-values.yaml")]
PEPPER = [
    "--set",
    "secrets.values.apiKeyPepper=" + "p" * 32,
    "--set",
    "secrets.values.webhookEncryptionKey=" + "w" * 32,
]

pytestmark = pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")

Manifest = dict[str, Any]


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the chart without its optional subchart, so rendering needs no download."""
    copy = tmp_path_factory.mktemp("chart") / "lorebase"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta))
    return copy


def render(chart: Path, *args: str) -> list[Manifest]:
    command = [str(HELM), "template", "ci", str(chart), "-n", "lorebase", *PEPPER, *GUIDE, *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def images(manifests: list[Manifest]) -> dict[str, str]:
    """Image of every Deployment, by component."""
    return {
        doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]: doc["spec"][
            "template"
        ]["spec"]["containers"][0]["image"]
        for doc in manifests
        if doc["kind"] == "Deployment"
    }


def test_scribe_and_guide_use_the_embedding_image(chart: Path) -> None:
    found = images(render(chart, "--set", "scribe.enabled=true", "--set", "image.tag=1.2.3"))
    assert found["guide"] == found["scribe"] == "ghcr.io/aantenore/lorebase-ml:1.2.3"
    assert found["keeper"] == "ghcr.io/aantenore/lorebase:1.2.3"


def test_embedding_image_can_be_left_out(chart: Path) -> None:
    found = images(
        render(
            chart,
            "--set",
            "scribe.enabled=true",
            "--set",
            "imageMl.repository=",
            "--set",
            "image.tag=1.2.3",
        )
    )
    assert (
        found["guide"] == found["scribe"] == found["keeper"] == "ghcr.io/aantenore/lorebase:1.2.3"
    )


def test_embedding_image_has_its_own_tag(chart: Path) -> None:
    found = images(render(chart, "--set", "imageMl.tag=@sha256:" + "a" * 64))
    assert found["guide"] == "ghcr.io/aantenore/lorebase-ml@sha256:" + "a" * 64

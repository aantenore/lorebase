"""Guide's state directory is writable in the Helm chart although its root file system is
read-only: an emptyDir by default, a PersistentVolumeClaim when guide.persistence is enabled."""

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


def guide_state(manifests: list[Manifest]) -> tuple[Manifest, Manifest]:
    """The Guide container and the volume mounted, writable, at its data directory."""
    spec = next(
        doc["spec"]["template"]["spec"]
        for doc in manifests
        if doc["kind"] == "Deployment"
        and doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "guide"
    )
    container = spec["containers"][0]
    data_dir = next(e["value"] for e in container["env"] if e["name"] == "LOREBASE__GUIDE__DATA_DIR")
    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == data_dir)
    assert not mount.get("readOnly")
    return container, next(v for v in spec["volumes"] if v["name"] == mount["name"])


def claims(manifests: list[Manifest]) -> list[Manifest]:
    return [m for m in manifests if m["kind"] == "PersistentVolumeClaim"]


def test_state_directory_is_an_empty_dir_by_default(chart: Path) -> None:
    manifests = render(chart)
    container, volume = guide_state(manifests)
    assert volume["emptyDir"] == {}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert not claims(manifests)


def test_state_directory_can_live_on_a_claim(chart: Path) -> None:
    manifests = render(chart, "--set", "guide.persistence.enabled=true")
    _, volume = guide_state(manifests)
    found = claims(manifests)
    assert [c["metadata"]["name"] for c in found] == [volume["persistentVolumeClaim"]["claimName"]]
    assert found[0]["spec"]["resources"]["requests"]["storage"] == "100Mi"


def test_state_directory_can_use_an_existing_claim(chart: Path) -> None:
    manifests = render(
        chart,
        "--set",
        "guide.persistence.enabled=true",
        "--set",
        "guide.persistence.existingClaim=guide-state",
    )
    _, volume = guide_state(manifests)
    assert volume["persistentVolumeClaim"]["claimName"] == "guide-state"
    assert not claims(manifests)

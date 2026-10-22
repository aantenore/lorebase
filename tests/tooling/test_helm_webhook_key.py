"""The chart seals webhook signing keys: Keeper and Scribe get webhooks.encryption_key from the
Secret, and the chart refuses to render without one unless unsealed keys are allowed."""

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
BASE = [
    "--set",
    "secrets.values.dbDsn=postgresql+asyncpg://lorebase_app:x@db.internal:5432/lorebase",
    "--set",
    "secrets.values.dbMaintenanceDsn=postgresql+asyncpg://lorebase:x@db.internal:5432/lorebase",
    "--set",
    "secrets.values.apiKeyPepper=" + "p" * 32,
    "--set",
    "scribe.enabled=true",
]
KEY = "w" * 40

pytestmark = pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the chart without its optional subchart, so rendering needs no download."""
    copy = tmp_path_factory.mktemp("chart") / "lorebase"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta))
    return copy


def helm(chart: Path, *sets: str) -> subprocess.CompletedProcess[str]:
    command = [str(HELM), "template", "ci", str(chart), "-n", "lorebase", *BASE]
    for value in sets:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def manifests(chart: Path, *sets: str) -> list[dict[str, Any]]:
    result = helm(chart, *sets)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def app_env(found: list[dict[str, Any]], component: str) -> dict[str, Any]:
    (deployment,) = [
        doc
        for doc in found
        if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith(f"-{component}")
    ]
    (container,) = [
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == component
    ]
    return {item["name"]: item for item in container["env"]}


def test_keeper_and_scribe_read_the_key_from_the_managed_secret(chart: Path) -> None:
    found = manifests(chart, f"secrets.values.webhookEncryptionKey={KEY}")
    (secret,) = [
        doc
        for doc in found
        if doc["kind"] == "Secret"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "secret"
    ]
    assert secret["stringData"]["webhook-encryption-key"] == KEY
    for component in ("keeper", "scribe"):
        env = app_env(found, component)
        reference = env["LOREBASE__WEBHOOKS__ENCRYPTION_KEY"]["value"]
        assert reference == "secret://env/LOREBASE_WEBHOOK_KEY"
        ref = env["LOREBASE_WEBHOOK_KEY"]["valueFrom"]["secretKeyRef"]
        assert (ref["name"], ref["key"]) == (secret["metadata"]["name"], "webhook-encryption-key")


def test_an_existing_secret_supplies_the_key(chart: Path) -> None:
    found = manifests(
        chart,
        "secrets.existingSecret=mine",
        "secrets.keys.webhookEncryptionKey=hook-key",
    )
    ref = app_env(found, "keeper")["LOREBASE_WEBHOOK_KEY"]["valueFrom"]["secretKeyRef"]
    assert (ref["name"], ref["key"]) == ("mine", "hook-key")


def test_the_chart_refuses_to_render_without_a_key(chart: Path) -> None:
    result = helm(chart)
    assert result.returncode != 0
    assert "secrets.values.webhookEncryptionKey" in result.stderr


def test_unsealed_keys_are_an_explicit_opt_out(chart: Path) -> None:
    found = manifests(chart, "config.webhooks.allow_unsealed_keys=true")
    assert "LOREBASE_WEBHOOK_KEY" not in app_env(found, "keeper")

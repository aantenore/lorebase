"""values.schema.json of the Helm chart declares every value and refuses wrong types and keys."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "lorebase"
PROFILES = CHART / "ci"
SCHEMA = json.loads((CHART / "values.schema.json").read_text())
HELM = shutil.which("helm")

Node = dict[str, Any]


def resolve(node: Node) -> Node:
    while "$ref" in node:
        node = SCHEMA["definitions"][node["$ref"].rsplit("/", 1)[1]]
    return node


def undeclared(values: Any, node: Node, path: str) -> list[str]:
    """Keys of `values` the schema does not declare, below objects that are closed."""
    node = resolve(node)
    if not isinstance(values, dict) or node.get("additionalProperties") is not False:
        return []
    missing = []
    for key, value in values.items():
        child = node.get("properties", {}).get(key)
        if child is None:
            missing.append(f"{path}.{key}")
        else:
            missing += undeclared(value, child, f"{path}.{key}")
    return missing


def test_every_default_value_is_declared() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert undeclared(values, SCHEMA, "") == []


@pytest.mark.parametrize("profile", sorted(p.name for p in PROFILES.glob("*-values.yaml")))
def test_every_profile_is_declared(profile: str) -> None:
    values = yaml.safe_load((PROFILES / profile).read_text())
    assert undeclared(values, SCHEMA, "") == []


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the chart without its optional subchart, so rendering needs no download."""
    copy = tmp_path_factory.mktemp("chart") / "lorebase"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta))
    return copy


@pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("keeper.replicaCount=two", "/keeper/replicaCount"),
        ("logLevel=VERBOSE", "/logLevel"),
        ("keeper.replicaCont=2", "replicaCont"),
        ("web.config.authMode=cookie", "/web/config/authMode"),
        ("telemetry.sampleRatio=2", "/telemetry/sampleRatio"),
    ],
)
def test_a_wrong_value_is_refused(chart: Path, value: str, message: str) -> None:
    command = [
        str(HELM), "template", "ci", str(chart),
        "-f", str(PROFILES / "minimal-values.yaml"), "--set", value,
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "schema" in result.stderr
    assert message in result.stderr

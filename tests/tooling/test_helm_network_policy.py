"""The Helm chart restricts the network of every workload by default (networkpolicy.yaml)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "lorebase"
PROFILES = CHART / "ci"
HELM = shutil.which("helm")
EVERYWHERE = {"0.0.0.0/0", "::/0"}
# The chart refuses to render without a database and a pepper; these make the defaults render.
REQUIRED = [
    "--set",
    "secrets.values.dbDsn=postgresql+asyncpg://lorebase_app:x@postgres.internal:5432/lorebase",
    "--set",
    "secrets.values.dbMaintenanceDsn=postgresql+asyncpg://lorebase:x@postgres.internal:5432/lorebase",
    "--set",
    "secrets.values.apiKeyPepper=" + "p" * 32,
]
VALUES = {
    "defaults": [],
    "minimal": ["minimal-values.yaml"],
    "production": ["production-values.yaml"],
    "scale-out": ["production-values.yaml", "scale-out-values.yaml"],
    "local-models": ["local-models-values.yaml"],
    "all": ["production-values.yaml", "scale-out-values.yaml", "local-models-values.yaml"],
}

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


def render(chart: Path, profile: str) -> list[Manifest]:
    args = [str(HELM), "template", "ci", str(chart), "-n", "lorebase", *REQUIRED]
    for name in VALUES[profile]:
        args += ["-f", str(PROFILES / name)]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def policies(manifests: list[Manifest]) -> list[Manifest]:
    return [doc for doc in manifests if doc["kind"] == "NetworkPolicy"]


def egress_rules(manifests: list[Manifest], component: str) -> list[Manifest]:
    rules: list[Manifest] = []
    for policy in policies(manifests):
        spec = policy["spec"]
        selected = spec["podSelector"].get("matchLabels", {})
        if "Egress" not in spec["policyTypes"]:
            continue
        if selected.get("app.kubernetes.io/component") == component:
            rules.extend(spec.get("egress") or [])
    return rules


def workloads(manifests: list[Manifest]) -> set[str]:
    """Components of the pods the release runs (Deployments, Jobs and the test Pod)."""
    found = set()
    for doc in manifests:
        if doc["kind"] in {"Deployment", "Job"}:
            found.add(doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"])
        elif doc["kind"] == "Pod":
            found.add(doc["metadata"]["labels"]["app.kubernetes.io/component"])
    return found


def ports(rule: Manifest) -> set[tuple[str, int]]:
    return {(port.get("protocol", "TCP"), port["port"]) for port in rule.get("ports", [])}


@pytest.mark.parametrize("profile", ["defaults", "production", "all"])
def test_every_workload_is_covered(chart: Path, profile: str) -> None:
    manifests = render(chart, profile)
    default = next(p for p in policies(manifests) if p["metadata"]["name"] == "ci-lorebase-default")
    assert set(default["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert default["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] == "ci"
    covered = {
        p["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/component")
        for p in policies(manifests)
        if "Egress" in p["spec"]["policyTypes"]
    }
    running = workloads(manifests)
    assert {"keeper", "migrate"} <= running
    assert running <= covered


@pytest.mark.parametrize("profile", sorted(VALUES))
def test_no_rule_opens_every_address(chart: Path, profile: str) -> None:
    for policy in policies(render(chart, profile)):
        for rule in policy["spec"].get("egress") or []:
            assert rule.get("to"), f"{policy['metadata']['name']}: an egress rule without peers"
            cidrs = {peer["ipBlock"]["cidr"] for peer in rule["to"] if "ipBlock" in peer}
            assert not cidrs & EVERYWHERE, policy["metadata"]["name"]


@pytest.mark.parametrize("profile", ["defaults", "production", "all"])
def test_dns_and_postgres_are_reachable(chart: Path, profile: str) -> None:
    manifests = render(chart, profile)
    for component in {"keeper", "scribe", "migrate"} & workloads(manifests):
        rules = egress_rules(manifests, component)
        dns = [r for r in rules if {("UDP", 53), ("TCP", 53)} <= ports(r)]
        assert dns, component
        assert dns[0]["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
        assert any(("TCP", 5432) in ports(r) for r in rules), component


def test_herald_and_test_pod_reach_keeper_only(chart: Path) -> None:
    manifests = render(chart, "production")
    for component in ("herald", "test"):
        peers = [
            peer["podSelector"]["matchLabels"]
            for rule in egress_rules(manifests, component)
            if ("TCP", 8080) in ports(rule)
            for peer in rule["to"]
        ]
        assert peers == [
            {
                "app.kubernetes.io/component": "keeper",
                "app.kubernetes.io/instance": "ci",
                "app.kubernetes.io/name": "lorebase",
            }
        ]
    assert egress_rules(manifests, "web") == []


def test_production_profile_narrows_egress_and_hides_the_docs(chart: Path) -> None:
    manifests = render(chart, "production")
    keeper = egress_rules(manifests, "keeper")
    database = next(r for r in keeper if ("TCP", 5432) in ports(r))
    assert database["to"] == [{"ipBlock": {"cidr": "10.0.0.0/16"}}]
    assert any(("TCP", 4317) in ports(r) for r in keeper)
    config = next(
        doc
        for doc in manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "ci-lorebase-config"
    )
    settings = yaml.safe_load(config["data"]["lorebase.yaml"])
    assert settings["keeper"]["openapi"]["docs_enabled"] is False


def test_in_cluster_postgres_is_selected_by_pod(chart: Path) -> None:
    manifests = render(chart, "local-models")
    for component in ("keeper", "scribe", "migrate"):
        rules = egress_rules(manifests, component)
        database = next(r for r in rules if ("TCP", 5432) in ports(r))
        cluster = {"cnpg.io/cluster": "ci-postgresql"}
        assert database["to"] == [{"podSelector": {"matchLabels": cluster}}]
    scribe = egress_rules(manifests, "scribe")
    assert any(("TCP", 11434) in ports(r) for r in scribe)

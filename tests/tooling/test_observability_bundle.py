"""The observability bundle (deploy/observability) agrees with the code and with the catalogue.

Checked here, without a running stack:

- every metric instrument in `packages/*/src` is in `docs/metrics.md`, with the Prometheus name
  that OpenTelemetry derives from it;
- every series named by a dashboard or an alert rule is in that catalogue;
- dashboards are well formed JSON, rules are well formed YAML with a runbook entry in
  `docs/operations.md` for each alert;
- compose and the Helm chart load the dashboards, the rules and the collector configuration.

`promtool`, from a Prometheus image run through docker, then parses the rules, every dashboard
expression and the Prometheus configuration; those tests are skipped where docker is missing or
the image can be neither found nor pulled, the structural checks always run. Set
`PROMTOOL_IMAGE` to use another image.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "observability"
DASHBOARDS = sorted((BUNDLE / "grafana" / "dashboards").glob("*.json"))
RULES = BUNDLE / "alerts" / "lorebase.rules.yml"
COLLECTOR = BUNDLE / "otel-collector.yaml"
PROMETHEUS = BUNDLE / "prometheus.yml"
CATALOGUE = ROOT / "docs" / "metrics.md"
OPERATIONS = ROOT / "docs" / "operations.md"
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
CHART = ROOT / "deploy" / "helm" / "lorebase"
HELM = shutil.which("helm")

PROMTOOL_IMAGE = os.environ.get("PROMTOOL_IMAGE", "prom/prometheus:latest")
PULL_TIMEOUT_S = 180
RUNBOOK_PREFIX = "https://github.com/aantenore/lorebase/blob/main/docs/operations.md#"
UNITS = {"s": "_seconds", "ms": "_milliseconds"}
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
INSTRUMENT = re.compile(
    r"create_(?:counter|histogram|gauge|up_down_counter|observable_\w+)\(\s*\"([^\"]+)\""
)
PROMQL_WORDS = {
    "and", "or", "unless", "on", "ignoring", "by", "without", "bool", "offset",
    "group_left", "group_right",
}  # fmt: skip
EXPECTED_DASHBOARDS = {"keeper.json", "store.json", "scribe.json"}
EXPECTED_ALERTS = {
    "LorebaseKeeperLatencyOverBudget",
    "LorebaseKeeperHighErrorRatio",
    "LorebaseReplicaLagHigh",
    "LorebaseJobFailureRatio",
    "LorebaseJobQueueStalled",
    "LorebaseWebhookDeliveryFailing",
    "LorebaseOutboxPruneBacklog",
    "LorebaseGcNotRun",
    "LorebaseAuditChainBroken",
}


# -- The catalogue -------------------------------------------------------------------------------


def catalogue() -> dict[str, dict[str, str]]:
    """The instrument tables of docs/metrics.md, by OpenTelemetry name."""
    found: dict[str, dict[str, str]] = {}
    for line in CATALOGUE.read_text().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 7 or not re.fullmatch(r"`[a-z][a-z0-9_.]*\.[a-z0-9_.]+`", cells[0]):
            continue
        series = re.match(r"`([a-z_:][a-z0-9_:]*)`", cells[1])
        assert series, f"no Prometheus series in the row of {cells[0]}"
        found[cells[0].strip("`")] = {
            "series": series.group(1),
            "type": cells[2],
            "unit": cells[3].strip("`"),
        }
    return found


def series_names(entries: dict[str, dict[str, str]]) -> set[str]:
    names: set[str] = set()
    for entry in entries.values():
        names.add(entry["series"])
        if entry["type"] == "histogram":
            names.update(entry["series"] + suffix for suffix in HISTOGRAM_SUFFIXES)
    return names


def instruments_in_code() -> set[str]:
    names: set[str] = set()
    for source in (ROOT / "packages").glob("*/src/**/*.py"):
        names.update(INSTRUMENT.findall(source.read_text()))
    return names


def test_catalogue_lists_every_instrument_of_the_code() -> None:
    documented = set(catalogue())
    emitted = instruments_in_code()
    assert emitted, "no instrument found in packages/*/src: the pattern is out of date"
    assert emitted - documented == set(), "instruments missing from docs/metrics.md"
    assert documented - emitted == set(), "docs/metrics.md lists instruments the code lacks"


def test_catalogue_series_follow_the_opentelemetry_conversion() -> None:
    for name, entry in catalogue().items():
        expected = name.replace(".", "_")
        if entry["unit"] in UNITS:
            expected += UNITS[entry["unit"]]
        if entry["type"] == "counter":
            expected += "_total"
        assert entry["series"] == expected, f"{name}: Prometheus series is {expected}"


# -- PromQL --------------------------------------------------------------------------------------


def metric_names(expr: str) -> set[str]:
    """The series names an expression selects (not functions, labels or keywords)."""
    text = re.sub(r"\$\{?\w+\}?", "1", expr)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"\{[^{}]*\}", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", "", text)
    names = set()
    for match in re.finditer(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\b(\s*\()?", text):
        if match.group(2) is None and match.group(1) not in PROMQL_WORDS:
            names.add(match.group(1))
    return names


def test_metric_name_extraction() -> None:
    expr = (
        'histogram_quantile(0.95, sum by (le) (rate(a_seconds_bucket{x=~"b|c"}[$__rate_interval])))'
        " / on() group_left sum without (z) (up) > 2 and absent(m_total)"
    )
    assert metric_names(expr) == {"a_seconds_bucket", "up", "m_total"}


# -- Dashboards ----------------------------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def panels_of(dashboard: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for panel in dashboard["panels"]:
        yield panel
        yield from panel.get("panels", [])


def expressions(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (f"{panel['title']} ({target['refId']})", target["expr"])
        for panel in panels_of(dashboard)
        for target in panel.get("targets", [])
    ]


def test_the_three_dashboards_exist() -> None:
    assert {path.name for path in DASHBOARDS} == EXPECTED_DASHBOARDS


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_is_well_formed(path: Path) -> None:
    dashboard = load(path)
    assert dashboard["uid"].startswith("lorebase-")
    assert dashboard["title"].startswith("Lorebase ")
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    assert variables["datasource"]["type"] == "datasource"
    panels = list(panels_of(dashboard))
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids)), "panel ids must be unique"
    for panel in panels:
        assert panel["title"], panel
        if panel["type"] in {"row", "text"}:
            continue
        assert panel["datasource"] == {"type": "prometheus", "uid": "${datasource}"}, panel["title"]
        refs = [target["refId"] for target in panel["targets"]]
        assert refs, panel["title"]
        assert len(refs) == len(set(refs)), panel["title"]
        assert all(target["expr"].strip() for target in panel["targets"]), panel["title"]


def test_dashboard_uids_and_titles_are_unique() -> None:
    boards = [load(path) for path in DASHBOARDS]
    assert len({board["uid"] for board in boards}) == len(boards)
    assert len({board["title"] for board in boards}) == len(boards)


def test_dashboards_cover_the_requested_views() -> None:
    text = {path.name: path.read_text() for path in DASHBOARDS}
    keeper, store, scribe = text["keeper.json"], text["store.json"], text["scribe.json"]
    for needle in (
        "p50 latency by route class",
        "p95 latency by route class",
        "5xx per second",
        "lorebase_http_rate_limited_total",
        "lorebase_auth_failures_total",
        "lorebase_keeper_cache_requests_total",
    ):
        assert needle in keeper, needle
    for needle in (
        "lorebase_db_replica_lag_milliseconds",
        "lorebase_db_reads_total",
        "lorebase_store_commit_duration_seconds_bucket",
        "lorebase_store_merge_attempts_total",
        "lorebase_store_reads_definer_total",
        "lorebase_gc_freed_bytes_total",
    ):
        assert needle in store, needle
    for needle in (
        "lorebase_scribe_jobs_total",
        "lorebase_scribe_source_rows_total",
        "lorebase_scribe_source_changes_total",
        "lorebase_jobs_queue_depth",
        "lorebase_jobs_claimed_total",
        "lorebase_webhooks_deliveries_total",
        "lorebase_extraction_items_total",
        "lorebase_gc_runs_total",
    ):
        assert needle in scribe, needle


def test_dashboards_use_only_catalogued_series() -> None:
    known = series_names(catalogue())
    for path in DASHBOARDS:
        for where, expr in expressions(load(path)):
            unknown = metric_names(expr) - known
            assert unknown == set(), f"{path.name}, {where}: not in docs/metrics.md: {unknown}"


# -- Alert rules ---------------------------------------------------------------------------------


def rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(RULES.read_text())
    return [rule for group in document["groups"] for rule in group["rules"]]


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower().replace("`", ""))
    return re.sub(r"\s", "-", text)


def operations_anchors() -> set[str]:
    return {
        slug(match.group(1))
        for match in re.finditer(r"^#{1,6}\s+(.+)$", OPERATIONS.read_text(), re.MULTILINE)
    }


def test_rule_file_structure() -> None:
    document = yaml.safe_load(RULES.read_text())
    assert [group["name"] for group in document["groups"]] == [
        "lorebase-keeper",
        "lorebase-store",
        "lorebase-scribe",
    ]
    found = rules()
    assert {rule["alert"] for rule in found} >= EXPECTED_ALERTS
    for rule in found:
        name = rule["alert"]
        assert re.fullmatch(r"Lorebase[A-Za-z]+", name), name
        assert rule["expr"].strip(), name
        assert rule["labels"]["severity"] in {"warning", "critical"}, name
        assert rule["labels"]["component"] in {"keeper", "store", "scribe"}, name
        for key in ("summary", "description", "runbook_url"):
            assert rule["annotations"][key].strip(), f"{name}: {key}"
        if "for" in rule:
            assert re.fullmatch(r"\d+[smhd]", rule["for"]), name
    classes = [r["labels"]["route_class"] for r in found if "route_class" in r["labels"]]
    assert len(classes) == len(set(classes)), "one latency rule per route class"


def test_every_alert_has_a_runbook_entry() -> None:
    anchors = operations_anchors()
    assert "dashboards-and-alerts" in anchors
    for rule in rules():
        url = rule["annotations"]["runbook_url"]
        assert url.startswith(RUNBOOK_PREFIX), url
        assert url.removeprefix(RUNBOOK_PREFIX) in anchors, f"{rule['alert']}: no entry for {url}"
        assert rule["alert"].lower() == url.removeprefix(RUNBOOK_PREFIX), rule["alert"]


def test_alerts_use_only_catalogued_series() -> None:
    known = series_names(catalogue())
    for rule in rules():
        unknown = metric_names(rule["expr"]) - known
        assert unknown == set(), f"{rule['alert']}: not in docs/metrics.md: {unknown}"


# -- Provisioning --------------------------------------------------------------------------------


def test_collector_exports_metrics_to_prometheus_and_traces_to_an_otlp_backend() -> None:
    config = yaml.safe_load(COLLECTOR.read_text())
    pipelines = config["service"]["pipelines"]
    assert pipelines["metrics"]["exporters"] == ["prometheus"]
    assert pipelines["traces"]["exporters"] == ["otlp/traces"]
    assert config["exporters"]["prometheus"]["endpoint"].endswith(":8889")
    assert config["exporters"]["prometheus"]["resource_to_telemetry_conversion"]["enabled"] is True
    assert "LOREBASE_TRACES_OTLP_ENDPOINT" in config["exporters"]["otlp/traces"]["endpoint"]
    for pipeline in pipelines.values():
        assert pipeline["receivers"] == ["otlp"]


def test_prometheus_loads_the_rules_and_compose_mounts_the_bundle() -> None:
    prometheus = yaml.safe_load(PROMETHEUS.read_text())
    assert prometheus["rule_files"] == ["/etc/prometheus/alerts/*.yml"]
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    mounts = {name: services[name]["volumes"] for name in ("prometheus", "grafana")}
    assert "../observability/alerts:/etc/prometheus/alerts:ro" in mounts["prometheus"]
    assert "../observability/grafana/dashboards:/etc/grafana/dashboards:ro" in mounts["grafana"]
    provider = yaml.safe_load(
        (BUNDLE / "grafana" / "provisioning" / "dashboards" / "dashboards.yaml").read_text()
    )["providers"][0]
    assert provider["options"]["path"] == "/etc/grafana/dashboards"


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the chart without its optional subchart, so rendering needs no download."""
    copy = tmp_path_factory.mktemp("chart") / "lorebase"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta))
    return copy


def render(chart: Path, *sets: str) -> list[dict[str, Any]]:
    assert HELM is not None
    args = [HELM, "template", "obs", str(chart), "-n", "lorebase"]
    args += [
        "--set",
        "secrets.values.dbDsn=postgresql+asyncpg://lorebase_app:x@db.internal:5432/lorebase",
        "--set",
        "secrets.values.dbMaintenanceDsn=postgresql+asyncpg://lorebase:x@db.internal:5432/lorebase",
    ]
    args += ["--set", "secrets.values.apiKeyPepper=" + "p" * 32]
    for value in sets:
        args += ["--set", value]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def config_maps(manifests: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    return [
        doc
        for doc in manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"].endswith(suffix)
    ]


@pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")
def test_chart_renders_the_bundle_only_when_enabled(chart: Path) -> None:
    for suffix in ("-dashboards", "-alert-rules", "-otel-collector"):
        assert config_maps(render(chart), suffix) == []

    manifests = render(chart, "observability.enabled=true")
    (dashboards,) = config_maps(manifests, "-dashboards")
    assert dashboards["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert set(dashboards["data"]) == EXPECTED_DASHBOARDS
    for path in DASHBOARDS:
        assert json.loads(dashboards["data"][path.name]) == load(path)
    (alert_rules,) = config_maps(manifests, "-alert-rules")
    assert yaml.safe_load(alert_rules["data"][RULES.name]) == yaml.safe_load(RULES.read_text())
    (collector,) = config_maps(manifests, "-otel-collector")
    assert yaml.safe_load(collector["data"]["config.yaml"]) == yaml.safe_load(COLLECTOR.read_text())


@pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")
def test_chart_parts_can_be_switched_off(chart: Path) -> None:
    manifests = render(
        chart,
        "observability.enabled=true",
        "observability.dashboards.enabled=false",
        "observability.collector.enabled=false",
    )
    assert config_maps(manifests, "-dashboards") == []
    assert config_maps(manifests, "-otel-collector") == []
    assert len(config_maps(manifests, "-alert-rules")) == 1


# -- promtool ------------------------------------------------------------------------------------

Promtool = Callable[[list[str], dict[Path, str]], subprocess.CompletedProcess[str]]


def _docker_image_ready(docker: str) -> bool:
    inspect = subprocess.run([docker, "image", "inspect", PROMTOOL_IMAGE], capture_output=True)
    if inspect.returncode == 0:
        return True
    try:
        pull = subprocess.run(
            [docker, "pull", "--quiet", PROMTOOL_IMAGE], capture_output=True, timeout=PULL_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return False
    return pull.returncode == 0


@pytest.fixture(scope="module")
def promtool() -> Promtool:
    """Runs `promtool` with the given files visible at the given paths."""
    docker = shutil.which("docker")
    if docker is None or not _docker_image_ready(docker):
        pytest.skip(f"the {PROMTOOL_IMAGE} image is not available (docker missing or no pull)")

    def containerised(args: list[str], mounts: dict[Path, str]) -> subprocess.CompletedProcess[str]:
        volumes = [arg for host, guest in mounts.items() for arg in ("-v", f"{host}:{guest}:ro")]
        command = [docker, "run", "--rm", "--entrypoint", "promtool", *volumes, PROMTOOL_IMAGE]
        return subprocess.run(
            [*command, *args], capture_output=True, text=True, check=False, timeout=120
        )

    return containerised


def test_promtool_accepts_the_alert_rules(promtool: Promtool) -> None:
    result = promtool(["check", "rules", "/rules/lorebase.rules.yml"], {RULES.parent: "/rules"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(rules())} rules found" in result.stdout


def test_promtool_accepts_the_prometheus_configuration(promtool: Promtool) -> None:
    mounts = {
        PROMETHEUS: "/etc/prometheus/prometheus.yml",
        RULES.parent: "/etc/prometheus/alerts",
    }
    result = promtool(["check", "config", "/etc/prometheus/prometheus.yml"], mounts)
    assert result.returncode == 0, result.stdout + result.stderr


def test_promtool_parses_every_dashboard_expression(promtool: Promtool, tmp_path: Path) -> None:
    recorded = []
    for path in DASHBOARDS:
        for index, (where, expr) in enumerate(expressions(load(path))):
            substituted = expr.replace("$__rate_interval", "5m")
            recorded.append({"record": f"dashboard:{path.stem}:{index}", "expr": substituted})
            assert "$" not in substituted, f"{path.name}, {where}: unsubstituted variable"
    assert recorded
    rules_file = tmp_path / "dashboards.rules.yml"
    rules_file.write_text(yaml.safe_dump({"groups": [{"name": "dashboards", "rules": recorded}]}))
    # The image runs promtool as an unprivileged user: let it read the private temporary dir.
    tmp_path.chmod(0o755)
    rules_file.chmod(0o644)
    result = promtool(["check", "rules", "/rules/dashboards.rules.yml"], {tmp_path: "/rules"})
    assert result.returncode == 0, result.stdout + result.stderr

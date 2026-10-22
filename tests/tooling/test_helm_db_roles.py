"""The Helm chart connects Keeper and Scribe as the application role and runs the migration and
the role setup as the maintenance role, as compose does (docs/deployment.md#database-roles)."""

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
PEPPER = ["--set", "secrets.values.apiKeyPepper=" + "p" * 32]
APP = "postgresql+asyncpg://lorebase_app:a@db.internal:5432/lorebase"
OWNER = "postgresql+asyncpg://lorebase:o@db.internal:5432/lorebase"

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


def helm(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [str(HELM), "template", "ci", str(chart), "-n", "lorebase", *PEPPER, *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render(chart: Path, *args: str) -> list[Manifest]:
    result = helm(chart, *args)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def pod(manifests: list[Manifest], kind: str, component: str) -> Manifest:
    return next(
        doc["spec"]["template"]["spec"]
        for doc in manifests
        if doc["kind"] == kind
        and doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]
        == component
    )


def env(container: Manifest) -> dict[str, Any]:
    """Environment of a container: a literal value, or (secret, key) of a secretKeyRef."""
    found: dict[str, Any] = {}
    for entry in container.get("env", []):
        ref = entry.get("valueFrom", {}).get("secretKeyRef")
        found[entry["name"]] = (ref["name"], ref["key"]) if ref else entry.get("value")
    return found


def containers(spec: Manifest) -> dict[str, Manifest]:
    return {c["name"]: c for c in [*spec.get("initContainers", []), *spec["containers"]]}


def test_external_database_uses_two_roles(chart: Path) -> None:
    manifests = render(
        chart, "-f", str(PROFILES / "minimal-values.yaml"), "--set", "scribe.enabled=true"
    )
    for component in ("keeper", "scribe"):
        found = env(pod(manifests, "Deployment", component)["containers"][0])
        assert found["LOREBASE_DB_DSN"] == ("ci-lorebase", "db-dsn"), component
        assert found["LOREBASE_DB_MAINTENANCE_DSN"] == ("ci-lorebase", "db-maintenance-dsn")
    job = containers(pod(manifests, "Job", "migrate"))
    migrate = env(job["migrate"])
    assert migrate["LOREBASE_DB_DSN"] == ("ci-lorebase-migrate", "db-maintenance-dsn")
    assert migrate["LOREBASE__DATABASE__APPLICATION_ROLE"] == "lorebase_app"
    roles = env(job["db-roles"])
    assert roles["LOREBASE_DB_DSN"] == ("ci-lorebase-migrate", "db-maintenance-dsn")
    assert roles["APP_DSN"] == ("ci-lorebase-migrate", "db-dsn")
    assert roles["CREATE_ROLE"] == "true"
    order = [c["name"] for c in pod(manifests, "Job", "migrate")["initContainers"]]
    assert order == ["wait-for-database", "db-roles"]
    secret = next(
        doc
        for doc in manifests
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "ci-lorebase"
    )
    assert set(secret["stringData"]) >= {"db-dsn", "db-maintenance-dsn"}


def test_existing_secret_keys(chart: Path) -> None:
    manifests = render(chart, "-f", str(PROFILES / "production-values.yaml"))
    keeper = env(pod(manifests, "Deployment", "keeper")["containers"][0])
    assert keeper["LOREBASE_DB_DSN"] == ("lorebase-secrets", "db-dsn")
    migrate = env(containers(pod(manifests, "Job", "migrate"))["migrate"])
    assert migrate["LOREBASE_DB_DSN"] == ("lorebase-secrets", "db-maintenance-dsn")


def test_keeper_can_go_without_the_maintenance_dsn(chart: Path) -> None:
    manifests = render(
        chart,
        "-f",
        str(PROFILES / "minimal-values.yaml"),
        "--set",
        "scribe.enabled=true",
        "--set",
        "database.maintenanceDsnInKeeper=false",
    )
    assert "LOREBASE_DB_MAINTENANCE_DSN" not in env(
        pod(manifests, "Deployment", "keeper")["containers"][0]
    )
    assert "LOREBASE_DB_MAINTENANCE_DSN" in env(
        pod(manifests, "Deployment", "scribe")["containers"][0]
    )


def test_in_cluster_database_uses_an_operator_managed_role(chart: Path) -> None:
    manifests = render(chart, "-f", str(PROFILES / "local-models-values.yaml"))
    keeper = env(pod(manifests, "Deployment", "keeper")["containers"][0])
    assert keeper["LOREBASE_DB_APP_PASSWORD"] == ("lorebase-db-app", "password")
    assert keeper["LOREBASE_DB_DSN"] == (
        "postgresql+asyncpg://lorebase_app:$(LOREBASE_DB_APP_PASSWORD)@ci-postgresql-rw:5432/lorebase"
    )
    assert keeper["LOREBASE_DB_MAINTENANCE_DSN"] == ("ci-postgresql-app", "uri")
    job = containers(pod(manifests, "Job", "migrate"))
    assert env(job["migrate"])["LOREBASE_DB_DSN"] == ("ci-postgresql-app", "uri")
    assert env(job["db-roles"])["CREATE_ROLE"] == "false"
    secret = next(
        doc
        for doc in manifests
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == "lorebase-db-app"
    )
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["stringData"]["username"] == "lorebase_app"


def test_shared_role_is_explicit(chart: Path) -> None:
    manifests = render(
        chart, "--set", f"secrets.values.dbDsn={OWNER}", "--set", "database.allowSharedRole=true"
    )
    keeper = env(pod(manifests, "Deployment", "keeper")["containers"][0])
    assert keeper["LOREBASE_DB_DSN"] == ("ci-lorebase", "db-dsn")
    assert "LOREBASE_DB_MAINTENANCE_DSN" not in keeper
    job = containers(pod(manifests, "Job", "migrate"))
    assert "db-roles" not in job
    migrate = env(job["migrate"])
    assert migrate["LOREBASE_DB_DSN"] == ("ci-lorebase-migrate", "db-dsn")
    assert "LOREBASE__DATABASE__APPLICATION_ROLE" not in migrate


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--set", f"secrets.values.dbDsn={APP}"], "no maintenance role"),
        (
            [
                "--set",
                f"secrets.values.dbDsn={OWNER}",
                "--set",
                f"secrets.values.dbMaintenanceDsn={OWNER}",
            ],
            "must differ",
        ),
        (
            [
                "--set",
                f"secrets.values.dbDsn={APP}",
                "--set",
                f"secrets.values.dbMaintenanceDsn={APP}",
            ],
            "must differ",
        ),
        (
            [
                "--set",
                "secrets.values.dbDsn=postgresql://other:a@db/l",
                "--set",
                f"secrets.values.dbMaintenanceDsn={OWNER}",
            ],
            "database.appRole.name",
        ),
        (
            [
                "-f",
                str(PROFILES / "local-models-values.yaml"),
                "--set",
                "database.appRole.name=lorebase",
            ],
            "must differ",
        ),
        (
            [
                "-f",
                str(PROFILES / "local-models-values.yaml"),
                "--set",
                "postgresql.cluster.roles=null",
            ],
            "must declare",
        ),
        (
            [
                "-f",
                str(PROFILES / "local-models-values.yaml"),
                "--set",
                "secrets.values.dbAppPassword=x",
            ],
            "URL-safe",
        ),
    ],
)
def test_unsafe_role_settings_are_refused(chart: Path, args: list[str], message: str) -> None:
    result = helm(chart, *args)
    assert result.returncode != 0
    assert message in result.stderr


def test_role_setup_script_is_shipped(chart: Path) -> None:
    manifests = render(chart, "-f", str(PROFILES / "minimal-values.yaml"))
    config = next(
        doc
        for doc in manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "ci-lorebase-migrate-config"
    )
    script = config["data"]["db-roles.py"]
    assert script.strip() == (CHART / "files" / "db-roles.py").read_text().strip()
    compile(script, "db-roles.py", "exec")

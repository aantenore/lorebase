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
PEPPER = [
    "--set",
    "secrets.values.apiKeyPepper=" + "p" * 32,
    "--set",
    "secrets.values.webhookEncryptionKey=" + "w" * 32,
]
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


@pytest.mark.parametrize(
    ("values", "backend"),
    [
        ([], None),  # one Keeper: the configuration decides (local)
        (["keeper.replicaCount=3"], "postgres"),
        (["keeper.autoscaling.enabled=true"], "postgres"),
        (["keeper.replicaCount=3", "config.keeper.limits.rate.backend=local"], None),
        (["keeper.replicaCount=3", "keeper.rateLimitBackend=local"], "local"),
    ],
)
def test_several_keepers_share_their_rate_limits(
    chart: Path, values: list[str], backend: str | None
) -> None:
    sets = [arg for value in values for arg in ("--set", value)]
    manifests = render(chart, "-f", str(PROFILES / "minimal-values.yaml"), *sets)
    keeper = env(pod(manifests, "Deployment", "keeper")["containers"][0])
    assert keeper.get("LOREBASE__KEEPER__LIMITS__RATE__BACKEND") == backend


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


def hooks(manifests: list[Manifest], component: str) -> dict[str, Manifest]:
    return {
        doc["kind"]: doc
        for doc in manifests
        if doc["metadata"].get("labels", {}).get("app.kubernetes.io/component") == component
        and "helm.sh/hook" in doc["metadata"].get("annotations", {})
    }


def weight(doc: Manifest) -> int:
    return int(doc["metadata"]["annotations"].get("helm.sh/hook-weight", "0"))


def test_in_cluster_roles_are_declared_before_the_migration(chart: Path) -> None:
    """One `helm upgrade` adds a role: the pg-roles hook runs before the migration Job."""
    manifests = render(chart, "-f", str(PROFILES / "local-models-values.yaml"))
    found = hooks(manifests, "pg-roles")
    assert set(found) == {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "ConfigMap",
        "NetworkPolicy",
        "Job",
    }
    for doc in found.values():
        annotations = doc["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-upgrade"
        assert "hook-succeeded" in annotations["helm.sh/hook-delete-policy"]
    job = found.pop("Job")
    assert all(weight(doc) < weight(job) for doc in found.values())
    migration = [
        doc
        for doc in manifests
        if doc["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "migrate"
        and "pre-upgrade" in doc["metadata"].get("annotations", {}).get("helm.sh/hook", "")
    ]
    assert {doc["kind"] for doc in migration} == {"ConfigMap", "Job"}
    assert all(weight(job) < weight(doc) for doc in migration)
    spec = job["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "ci-lorebase-pg-roles"
    assert spec["automountServiceAccountToken"] is True
    settings = env(spec["containers"][0])
    assert settings["CLUSTER"] == "ci-postgresql"
    assert settings["APP_ROLE"] == "lorebase_app"
    assert [role["name"] for role in yaml.safe_load(settings["ROLES"])] == ["lorebase_app"]
    rules = found["Role"]["rules"]
    assert {(rule["resources"][0], tuple(rule["resourceNames"])) for rule in rules} == {
        ("clusters", ("ci-postgresql",)),
        ("secrets", ("lorebase-db-app",)),
    }
    secrets = {verb for rule in rules if rule["resources"] == ["secrets"] for verb in rule["verbs"]}
    assert secrets == {"get"}
    script = found["ConfigMap"]["data"]["pg-roles.py"]
    assert script.strip() == (CHART / "files" / "pg-roles.py").read_text().strip()
    compile(script, "pg-roles.py", "exec")


def test_role_setup_of_an_upgrade_starts_before_the_password_secret(chart: Path) -> None:
    """The Secret of a role added by an upgrade is created after the hooks; the Job still starts."""
    manifests = render(chart, "-f", str(PROFILES / "local-models-values.yaml"))
    job = containers(pod(manifests, "Job", "migrate"))["db-roles"]
    ref = next(e for e in job["env"] if e["name"] == "LOREBASE_DB_APP_PASSWORD")
    assert ref["valueFrom"]["secretKeyRef"]["optional"] is True
    keeper = pod(manifests, "Deployment", "keeper")["containers"][0]
    ref = next(e for e in keeper["env"] if e["name"] == "LOREBASE_DB_APP_PASSWORD")
    assert "optional" not in ref["valueFrom"]["secretKeyRef"]


IN_CLUSTER = ["-f", str(PROFILES / "local-models-values.yaml")]


@pytest.mark.parametrize(
    "args",
    [
        ["-f", str(PROFILES / "minimal-values.yaml")],
        [*IN_CLUSTER, "--set", "database.setup.clusterRoles.enabled=false"],
        [*IN_CLUSTER, "--set", "database.allowSharedRole=true"],
        [*IN_CLUSTER, "--set", "migrate.hookEventsInCluster={post-install,post-upgrade}"],
    ],
)
def test_role_hook_only_where_it_helps(chart: Path, args: list[str]) -> None:
    assert not hooks(render(chart, *args), "pg-roles")


def test_role_hook_needs_a_route_to_the_api_server(chart: Path) -> None:
    result = helm(chart, *IN_CLUSTER, "--set", "networkPolicy.egress.kubernetesApi.to=null")
    assert result.returncode != 0
    assert "networkPolicy.egress.kubernetesApi.to" in result.stderr


def test_the_roles_hook_may_read_every_role_password_secret(chart: Path, tmp_path: Path) -> None:
    """Each role of `postgresql.cluster.roles` may name its own password Secret; the hook
    checks whether each exists, so its Role grants `get` on all of them."""
    profile = yaml.safe_load((PROFILES / "local-models-values.yaml").read_text())
    roles = profile.get("postgresql", {}).get("cluster", {}).get("roles")
    if roles is None:
        values = yaml.safe_load((chart / "values.yaml").read_text())
        roles = values["postgresql"]["cluster"]["roles"]
    extra = {
        **roles[0],
        "name": "lorebase_report",
        "passwordSecret": {"name": "lorebase-db-report"},
    }
    overrides = tmp_path / "roles.yaml"
    overrides.write_text(yaml.safe_dump({"postgresql": {"cluster": {"roles": [*roles, extra]}}}))
    manifests = render(
        chart, "-f", str(PROFILES / "local-models-values.yaml"), "-f", str(overrides)
    )
    role = hooks(manifests, "pg-roles")["Role"]
    secrets = next(rule for rule in role["rules"] if rule["resources"] == ["secrets"])
    assert secrets["resourceNames"] == ["lorebase-db-app", "lorebase-db-report"]

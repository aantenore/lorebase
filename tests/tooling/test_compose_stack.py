"""The compose stack keeps its services on the roles and secrets it documents.

The file is rendered by `docker compose config` (interpolation only, no daemon needed); the
test is skipped where the docker CLI is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
ROLE_SCRIPT = ROOT / "deploy" / "compose" / "postgres" / "app-role.sh"
BASE_ENV = {
    "POSTGRES_USER": "owner",
    "POSTGRES_PASSWORD": "owner-secret",
    "POSTGRES_DB": "lorebase",
    "LOREBASE_APP_USER": "app",
    "LOREBASE_APP_PASSWORD": "app-secret",
}
APP_SERVICES = ("keeper", "keeper-2", "scribe")


def render(env: dict[str, str], *profiles: str) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not installed")
    args = [docker, "compose", "--env-file", os.devnull, "-f", str(COMPOSE_FILE)]
    args += ["--project-name", "lorebase-test"]
    for profile in profiles:
        args += ["--profile", profile]
    # Only the variables given here: no .env file of the developer, no ambient values.
    kept = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME"} or k.startswith("DOCKER_")}
    clean = {**kept, **env}
    return subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=ROOT,
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def services(env: dict[str, str], *profiles: str) -> dict[str, Any]:
    result = render(env, *profiles)
    if result.returncode != 0 and "docker compose" in result.stderr and "unknown" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    found: dict[str, Any] = json.loads(result.stdout)["services"]
    return found


def dsn_user(service: dict[str, Any]) -> str | None:
    dsn: str = service["environment"]["LOREBASE_DB_DSN"]
    return urlsplit(dsn).username


def test_keeper_and_scribe_connect_as_the_application_role() -> None:
    found = services(BASE_ENV, "scale-out")
    for name in APP_SERVICES:
        assert dsn_user(found[name]) == "app", name
        # Index maintenance stays on the owner of the tables.
        maintenance: str = found[name]["environment"]["LOREBASE_DB_MAINTENANCE_DSN"]
        assert urlsplit(maintenance).username == "owner", name


def test_migrations_run_as_the_owner_and_grant_the_application_role() -> None:
    migrate = services(BASE_ENV)["migrate"]
    assert dsn_user(migrate) == "owner"
    assert migrate["environment"]["LOREBASE__DATABASE__APPLICATION_ROLE"] == "app"
    assert migrate["depends_on"]["db-roles"]["condition"] == "service_completed_successfully"


def test_the_application_password_is_required() -> None:
    env = {key: value for key, value in BASE_ENV.items() if key != "LOREBASE_APP_PASSWORD"}
    result = render(env)
    assert result.returncode != 0
    assert "LOREBASE_APP_PASSWORD" in result.stderr


def test_the_application_role_is_created_without_privileges_that_skip_rls() -> None:
    script = ROLE_SCRIPT.read_text()
    assert "NOSUPERUSER NOBYPASSRLS" in script
    roles = services(BASE_ENV)["db-roles"]
    assert roles["environment"]["APP_USER"] == "app"
    assert roles["environment"]["POSTGRES_USER"] == "owner"


GUARD_SCRIPT = ROOT / "deploy" / "compose" / "require-secrets.sh"
PROFILE_SECRETS = {
    # profile: service, the variable it reads the secret from, the guard, the secret
    "litellm": ("litellm", "LITELLM_MASTER_KEY", "litellm-secrets", "LITELLM_MASTER_KEY"),
    "observability": (
        "grafana",
        "GF_SECURITY_ADMIN_PASSWORD",
        "grafana-secrets",
        "GRAFANA_ADMIN_PASSWORD",
    ),
}


@pytest.mark.parametrize("profile", sorted(PROFILE_SECRETS))
def test_profile_secrets_have_no_default_and_are_guarded(profile: str) -> None:
    service, variable, guard_service, secret = PROFILE_SECRETS[profile]
    found = services(BASE_ENV, profile)
    assert found[service]["environment"][variable] in ("", None)
    condition = found[service]["depends_on"][guard_service]["condition"]
    assert condition == "service_completed_successfully"
    assert found[guard_service]["command"] == [secret]
    assert found[guard_service]["environment"][secret] in ("", None)


def guard(value: str | None) -> subprocess.CompletedProcess[str]:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX shell")
    env = {"PATH": os.environ.get("PATH", "")}
    if value is not None:
        env["SOME_SECRET"] = value
    return subprocess.run(
        [sh, str(GUARD_SCRIPT), "SOME_SECRET"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("value", [None, "", "change-me", "short-secret"])
def test_the_guard_refuses_missing_placeholder_and_short_secrets(value: str | None) -> None:
    result = guard(value)
    assert result.returncode == 1
    assert "SOME_SECRET" in result.stderr


def test_the_guard_accepts_a_generated_secret() -> None:
    assert guard("sk-" + "0123456789abcdef" * 3).returncode == 0

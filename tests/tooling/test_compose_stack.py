"""The compose stack keeps its services on the roles and secrets it documents.

The file is rendered by `docker compose config` (interpolation only, no daemon needed); the
test is skipped where the docker CLI is missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from lorebase_store.definer import READ_FUNCTIONS

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
ROLE_SCRIPT = ROOT / "deploy" / "compose" / "postgres" / "app-role.sh"
OIDC_MOCK = ROOT / "deploy" / "compose" / "oidc-mock.yaml"
BASE_ENV = {
    "POSTGRES_USER": "owner",
    "POSTGRES_PASSWORD": "owner-secret",
    "POSTGRES_DB": "lorebase",
    "LOREBASE_APP_USER": "app",
    "LOREBASE_APP_PASSWORD": "app-secret",
    "LOREBASE_WEBHOOK_KEY": "w" * 64,
}
APP_SERVICES = ("keeper", "keeper-2", "scribe")


def render(
    env: dict[str, str], *profiles: str, overlays: tuple[Path, ...] = ()
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not installed")
    args = [docker, "compose", "--env-file", os.devnull, "-f", str(COMPOSE_FILE)]
    for overlay in overlays:
        args += ["-f", str(overlay)]
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


def services(
    env: dict[str, str], *profiles: str, overlays: tuple[Path, ...] = ()
) -> dict[str, Any]:
    result = render(env, *profiles, overlays=overlays)
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


def test_webhook_signing_keys_are_sealed_by_keeper_and_scribe() -> None:
    found = services(BASE_ENV, "scale-out")
    for name in APP_SERVICES:
        env = found[name]["environment"]
        assert env["LOREBASE__WEBHOOKS__ENCRYPTION_KEY"] == "secret://env/LOREBASE_WEBHOOK_KEY"
        assert env["LOREBASE_WEBHOOK_KEY"] == "w" * 64, name


def test_the_webhook_key_is_required() -> None:
    env = {key: value for key, value in BASE_ENV.items() if key != "LOREBASE_WEBHOOK_KEY"}
    result = render(env)
    assert result.returncode != 0
    assert "LOREBASE_WEBHOOK_KEY" in result.stderr


def test_the_application_password_is_required() -> None:
    env = {key: value for key, value in BASE_ENV.items() if key != "LOREBASE_APP_PASSWORD"}
    result = render(env)
    assert result.returncode != 0
    assert "LOREBASE_APP_PASSWORD" in result.stderr


def test_the_application_role_is_created_without_privileges_that_skip_rls() -> None:
    script = ROLE_SCRIPT.read_text()
    assert "NOSUPERUSER NOBYPASSRLS" in script
    # Re-granting row rights on every start must not undo revision 0015.
    grant = script.index("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES")
    assert script.index("REVOKE UPDATE, DELETE, TRUNCATE") > grant
    assert "public.audit_log" in script
    assert "public.audit_read_log" in script
    roles = services(BASE_ENV)["db-roles"]
    assert roles["environment"]["APP_USER"] == "app"
    assert roles["environment"]["POSTGRES_USER"] == "owner"


def test_the_role_script_grants_every_definer_function_from_the_catalog() -> None:
    script = ROLE_SCRIPT.read_text()
    # No list of names to keep in step with the migrations and the Helm db-roles.py.
    assert "p.prosecdef" in script
    assert not re.search(r"proname\s*(=|IN|ANY)", script)
    assert not [name for name in READ_FUNCTIONS if name in script]


@pytest.mark.skipif(not shutil.which("sh"), reason="no POSIX shell")
def test_the_role_script_is_valid_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(ROLE_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


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


WEB_TEMPLATE = ROOT / "deploy" / "compose" / "web" / "proxy.conf.template"
WEB_DEFAULT_CONF = ROOT / "deploy" / "docker" / "web" / "default.conf"


def test_web_is_part_of_the_default_stack_on_loopback() -> None:
    web = services(BASE_ENV)["web"]
    assert "profiles" not in web
    assert web["depends_on"]["keeper"]["condition"] == "service_healthy"
    (port,) = web["ports"]
    assert (port["host_ip"], port["published"], port["target"]) == ("127.0.0.1", "8090", 8080)
    assert web["build"]["dockerfile"] == "deploy/docker/web.Dockerfile"


def test_web_port_and_bind_are_configurable() -> None:
    web = services({**BASE_ENV, "LOREBASE_WEB_PORT": "18090", "WEB_BIND": "127.0.0.2"})["web"]
    (port,) = web["ports"]
    assert (port["host_ip"], port["published"]) == ("127.0.0.2", "18090")


def test_web_runs_the_app_against_its_own_origin() -> None:
    env = services(BASE_ENV)["web"]["environment"]
    assert env["VITE_API_MODE"] == "live"
    # A path: the browser calls the origin that served the app, the CSP stays on 'self'.
    assert env["VITE_KEEPER_BASE_URL"] == "/"
    assert env["VITE_GUIDE_URL"] in ("", None)
    assert env["LOREBASE_WEB_PROXY_KEEPER_PORT"] == "8080"


def test_web_proxy_follows_the_keeper_and_guide_ports() -> None:
    env = services({**BASE_ENV, "KEEPER_PORT": "9000", "GUIDE_PORT": "9500"})["web"]["environment"]
    assert env["LOREBASE_WEB_PROXY_KEEPER_PORT"] == "9000"
    assert env["LOREBASE_WEB_PROXY_GUIDE_PORT"] == "9500"


def test_guide_accepts_the_origin_of_the_web_service() -> None:
    guide = services(BASE_ENV, "assistant")["guide"]
    origins = json.loads(guide["environment"]["LOREBASE__GUIDE__HTTP__ALLOWED_ORIGINS"])
    assert origins == ["http://127.0.0.1:8090", "http://localhost:8090"]
    # An empty GUIDE_ALLOWED_ORIGINS list falls back to these in Guide.
    defaults = json.loads(guide["environment"]["LOREBASE__GUIDE__HTTP__DEFAULT_ORIGINS"])
    assert defaults == origins


def test_guide_keeps_a_read_only_root_with_a_named_volume_for_its_state() -> None:
    guide = services(BASE_ENV, "assistant")["guide"]
    assert guide["read_only"] is True
    data_dir = guide["environment"]["LOREBASE__GUIDE__DATA_DIR"]
    mounts = {v["target"]: v for v in guide["volumes"]}
    assert mounts[data_dir]["type"] == "volume"
    assert mounts[data_dir]["source"] == "guide-data"
    assert not mounts[data_dir].get("read_only")


def test_web_proxy_template_forwards_api_and_guide_without_buffering() -> None:
    text = WEB_TEMPLATE.read_text()
    for prefix, upstream in (("/api/", "keeper"), ("/guide/", "guide")):
        block = text[text.index(f"location {prefix} {{") :].split("\n}\n")[0]
        assert f"http://{upstream}:${{LOREBASE_WEB_PROXY_" in block
        assert "proxy_buffering off;" in block
        assert "proxy_http_version 1.1;" in block
    # Only the prefix of Guide is removed; Keeper serves its API under /api/v1.
    assert "rewrite ^/guide/(.*)$ /$1 break;" in text
    # Every substituted variable is one the service passes (nothing renders empty).
    names = set(re.findall(r"\$\{(LOREBASE_WEB_PROXY_\w+)\}", text))
    assert names <= set(services(BASE_ENV)["web"]["environment"])


def test_web_image_includes_the_directory_the_proxy_is_rendered_into() -> None:
    assert "include /etc/nginx/conf.d/lorebase.d/*.conf;" in WEB_DEFAULT_CONF.read_text()
    env = services(BASE_ENV)["web"]["environment"]
    assert env["NGINX_ENVSUBST_OUTPUT_DIR"] == "/etc/nginx/conf.d/lorebase.d"
    assert env["NGINX_ENVSUBST_FILTER"] == "^LOREBASE_WEB_PROXY_"


def test_oidc_mock_overlay_signs_in_through_the_web_service() -> None:
    env = {**BASE_ENV, "LOREBASE_SESSION_KEY": "k" * 48, "LOREBASE_WEB_PORT": "18090"}
    found = services(env, overlays=(OIDC_MOCK,))
    keeper = found["keeper"]["environment"]
    assert keeper["LOREBASE__KEEPER__AUTH__MODE"] == "oidc"
    assert keeper["LOREBASE__KEEPER__AUTH__SESSIONS__ENABLED"] == "true"
    # One loopback issuer for the browser and for Keeper: the provider shares Keeper's network.
    assert keeper["LOREBASE__KEEPER__AUTH__OIDC__ISSUER"] == "http://localhost:8096/lorebase"
    assert found["oidc-mock"]["network_mode"] == "service:keeper"
    published = {(port["published"], port["target"]) for port in found["keeper"]["ports"]}
    assert ("8096", 8096) in published
    callback = keeper["LOREBASE__KEEPER__AUTH__SESSIONS__REDIRECT_URI"]
    assert callback == "http://127.0.0.1:18090/api/v1/auth/callback"
    assert found["web"]["environment"]["VITE_AUTH_MODE"] == "session"


SEARCH_BM25 = ROOT / "deploy" / "compose" / "search-bm25.yaml"


def test_bm25_overlay_runs_postgres_on_paradedb_with_pg_search_preloaded() -> None:
    env = {**BASE_ENV, "POSTGRES_PRELOAD_LIBRARIES": "pg_prewarm"}
    found = services(env, "scale-out", "maintenance", overlays=(SEARCH_BM25,))
    postgres = found["postgres"]
    assert postgres["image"].startswith("paradedb/paradedb:0.26.3-pg16@sha256:")
    assert "shared_preload_libraries=pg_search,pg_prewarm" in postgres["command"]
    assert "/docker-entrypoint-initdb.d" in postgres["tmpfs"]
    replica = found["postgres-replica"]
    assert replica["image"] == postgres["image"] == found["replica-prep"]["image"]
    assert replica["command"] == ["-c", "shared_preload_libraries=pg_search,pg_prewarm"]
    for name in ("keeper", "keeper-2", "storage"):
        environment = found[name]["environment"]
        assert environment["LOREBASE__SEARCH__BACKEND"] == "bm25", name
        assert environment["LOREBASE__STORE__STORAGE__BM25_INDEX"] == "true", name
    alone = services(BASE_ENV, overlays=(SEARCH_BM25,))["postgres"]["command"]
    assert "shared_preload_libraries=pg_search" in alone
    # The default stack keeps the pgvector image and the default ranking.
    default = services(BASE_ENV)
    assert default["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert "LOREBASE__SEARCH__BACKEND" not in default["keeper"]["environment"]

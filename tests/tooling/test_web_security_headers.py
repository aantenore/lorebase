"""The web image and the Helm chart send the same security headers, with a Content-Security-Policy
that allows the Keeper origin from the runtime configuration."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "docker" / "web" / "40-lorebase-config.sh"
CHART = ROOT / "deploy" / "helm" / "lorebase"
HELM = shutil.which("helm")
CSP_PREFIX = "Content-Security-Policy "
REQUIRED = [
    "--set",
    "secrets.values.dbDsn=postgresql+asyncpg://lorebase_app:x@postgres.internal:5432/lorebase",
    "--set",
    "secrets.values.dbMaintenanceDsn=postgresql+asyncpg://lorebase:x@postgres.internal:5432/lorebase",
    "--set",
    "secrets.values.apiKeyPepper=" + "p" * 32,
    "--set",
    "secrets.values.webhookEncryptionKey=" + "w" * 32,
    "--set",
    "web.enabled=true",
]


def run_script(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("VITE_", "LOREBASE_"))}
    clean["LOREBASE_WEB_HEADERS_FILE"] = str(tmp_path / "headers.inc")
    clean["LOREBASE_WEB_CONFIG_FILE"] = str(tmp_path / "config.js")
    clean.update(env)
    return subprocess.run(
        ["sh", str(SCRIPT)], env=clean, capture_output=True, text=True, check=False
    )


def directives(text: str) -> dict[str, str]:
    """The `add_header` directives of an nginx include, by header name."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("add_header "):
            name, _, rest = line.removeprefix("add_header ").partition(" ")
            found[name] = rest.removesuffix(" always;").strip('"')
    return found


def sources(policy: str, directive: str) -> list[str]:
    for part in policy.split(";"):
        name, _, value = part.strip().partition(" ")
        if name == directive:
            return value.split()
    return []


def test_default_policy_without_a_keeper(tmp_path: Path) -> None:
    result = run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    headers = directives((tmp_path / "headers.inc").read_text())
    policy = headers["Content-Security-Policy"]
    assert sources(policy, "default-src") == ["'self'"]
    assert sources(policy, "script-src") == ["'self'"]
    assert sources(policy, "connect-src") == ["'self'"]
    assert sources(policy, "worker-src") == ["'self'", "blob:"]
    assert sources(policy, "frame-ancestors") == ["'none'"]
    assert sources(policy, "object-src") == ["'none'"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in headers
    assert not (tmp_path / "config.js").exists()


def test_keeper_origin_comes_from_the_base_url(tmp_path: Path) -> None:
    result = run_script(
        tmp_path,
        VITE_API_MODE="live",
        VITE_KEEPER_BASE_URL="https://keeper.example.com:8443/some/path?x=1",
        LOREBASE_WEB_CSP_CONNECT_SRC="https://other.example wss://events.example",
        LOREBASE_WEB_HSTS="max-age=31536000",
    )
    assert result.returncode == 0, result.stderr
    headers = directives((tmp_path / "headers.inc").read_text())
    assert sources(headers["Content-Security-Policy"], "connect-src") == [
        "'self'",
        "https://keeper.example.com:8443",
        "https://other.example",
        "wss://events.example",
    ]
    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "VITE_KEEPER_BASE_URL" in (tmp_path / "config.js").read_text()


def test_relative_base_url_adds_no_origin(tmp_path: Path) -> None:
    run_script(tmp_path, VITE_KEEPER_BASE_URL="/")
    headers = directives((tmp_path / "headers.inc").read_text())
    assert sources(headers["Content-Security-Policy"], "connect-src") == ["'self'"]


def test_policy_override(tmp_path: Path) -> None:
    run_script(tmp_path, LOREBASE_WEB_CSP="default-src 'none'", VITE_KEEPER_BASE_URL="https://k.io")
    headers = directives((tmp_path / "headers.inc").read_text())
    assert headers["Content-Security-Policy"] == "default-src 'none'"


@pytest.mark.parametrize("value", ['a"b', "a$b", "a\\b", "a\nb"])
def test_unsafe_values_stop_the_container(tmp_path: Path, value: str) -> None:
    result = run_script(tmp_path, LOREBASE_WEB_CSP=value)
    assert result.returncode != 0
    assert not (tmp_path / "headers.inc").exists()


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the chart without its optional subchart, so rendering needs no download."""
    copy = tmp_path_factory.mktemp("chart") / "lorebase"
    shutil.copytree(CHART, copy, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta))
    return copy


def render_nginx(chart: Path, *sets: str) -> str:
    args = [
        str(HELM),
        "template",
        "ci",
        str(chart),
        "-s",
        "templates/web-configmap.yaml",
        *REQUIRED,
    ]
    for item in sets:
        args += ["--set", item]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return str(yaml.safe_load(result.stdout)["data"]["default.conf"])


needs_helm = pytest.mark.skipif(HELM is None, reason="the helm binary is not installed")


@needs_helm
def test_chart_sends_the_headers_from_every_location(chart: Path) -> None:
    conf = render_nginx(
        chart,
        "web.config.keeperBaseUrl=https://keeper.example.com/api",
        "web.security.connectSrc={https://other.example}",
    )
    assert conf.count("add_header Content-Security-Policy") == 3
    line = next(line for line in conf.splitlines() if CSP_PREFIX in line)
    policy = line.split(CSP_PREFIX)[1].rsplit('" always', 1)[0].strip('"')
    assert sources(policy, "connect-src") == [
        "'self'",
        "https://keeper.example.com",
        "https://other.example",
    ]
    assert "Strict-Transport-Security" not in conf


@needs_helm
def test_chart_policy_override_and_hsts(chart: Path) -> None:
    conf = render_nginx(
        chart,
        "web.config.keeperBaseUrl=/",
        "web.security.csp=default-src 'none'",
        "web.security.hsts=max-age=60",
    )
    assert conf.count("Content-Security-Policy \"default-src 'none'\" always;") == 3
    assert conf.count('Strict-Transport-Security "max-age=60" always;') == 3

"""The quickstart helpers: `scripts/quickstart.sh env` and the hosted model compose overlay.

The stack commands of the script (up, data, down, clean) need Docker and a running Keeper; they
are exercised by the fresh-clone check in docs/validation.md, not here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "quickstart.sh"
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
HOSTED_FILE = ROOT / "deploy" / "compose" / "hosted-model.yaml"
PASSWORDS = ("POSTGRES_PASSWORD", "LOREBASE_APP_PASSWORD")
GENERATED = (*PASSWORDS, "LOREBASE_WEBHOOK_KEY")


def run_env(env_file: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None or shutil.which("openssl") is None:
        pytest.skip("bash or openssl not installed")
    return subprocess.run(
        ["bash", str(SCRIPT), "env"],
        env={"PATH": os.environ["PATH"], "LOREBASE_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def values(env_file: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    return {k: v for k, v in pairs if not k.startswith("#")}


def test_env_creates_the_file_with_generated_passwords_and_webhook_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    run_env(env_file)

    got = values(env_file)
    expected = values(ROOT / ".env.example")
    for name in PASSWORDS:
        assert re.fullmatch(r"[0-9a-f]{48}", got[name]), name
    assert got["POSTGRES_PASSWORD"] != got["LOREBASE_APP_PASSWORD"]
    assert re.fullmatch(r"[0-9a-f]{64}", got["LOREBASE_WEBHOOK_KEY"])
    assert {k: v for k, v in got.items() if k not in GENERATED} == {
        k: v for k, v in expected.items() if k not in GENERATED
    }
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_env_keeps_set_values_and_fills_placeholders(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=change-me\nLOREBASE_APP_PASSWORD=mine\nKEEPER_HOST_PORT=18080\n"
    )
    run_env(env_file)
    first = values(env_file)
    assert re.fullmatch(r"[0-9a-f]{48}", first["POSTGRES_PASSWORD"])
    assert first["LOREBASE_APP_PASSWORD"] == "mine"
    assert first["KEEPER_HOST_PORT"] == "18080"
    # A file from before the webhook key gets one, appended.
    assert re.fullmatch(r"[0-9a-f]{64}", first["LOREBASE_WEBHOOK_KEY"])

    run_env(env_file)
    assert values(env_file) == first


def test_up_starts_the_web_service_and_prints_its_url() -> None:
    text = SCRIPT.read_text()
    assert "local args=(postgres migrate keeper web)" in text
    assert "LOREBASE_WEB_PORT" in text
    env = values(ROOT / ".env.example")
    assert env["LOREBASE_WEB_PORT"] != env["KEEPER_HOST_PORT"]


def run_script(
    *args: str, profiles: str | None = None, env_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash not installed")
    env = {"PATH": os.environ["PATH"]}
    if profiles is not None:
        env["PROFILES"] = profiles
    if env_file is not None:
        env["LOREBASE_ENV_FILE"] = str(env_file)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_profiles_other_than_assistant_are_refused(tmp_path: Path) -> None:
    result = run_script("up", profiles="assistant observability", env_file=tmp_path / ".env")
    assert result.returncode == 2
    assert "observability is not supported" in result.stderr


def test_up_adds_guide_and_scribe_with_the_assistant_profile() -> None:
    text = SCRIPT.read_text()
    assert "if has_profile assistant; then args+=(guide scribe); fi" in text
    assert "assistant.yaml" in text
    assert (COMPOSE_FILE.parent / "assistant.yaml").is_file()


def test_down_stops_every_profile_up_can_start() -> None:
    text = SCRIPT.read_text()
    down = text[text.index("  down)") : text.index("  clean)")]
    assert 'compose --profile "*" down' in down


def test_makefile_passes_profiles_to_up() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert 'PROFILES="$(PROFILES)" scripts/quickstart.sh up' in makefile
    assert "up-assistant:" in makefile
    assert "PROFILES=assistant scripts/quickstart.sh up" in makefile
    assert "example-documents:" in makefile


def test_example_documents_exist_and_share_a_suggestion() -> None:
    text = SCRIPT.read_text()
    for path in re.findall(r'"\$ROOT/(examples/[^"]+\.md)"', text):
        assert (ROOT / path).is_file(), path
    # `buyer` of C-3099 and C-3025 is what two documents support (schema.suggestions.min_support).
    for name in ("five_day_demo/documents/contract-c-3099", "supply/documents/contract-c-3025"):
        assert any((ROOT / "examples").glob(f"{name}*.md"))


def test_unknown_command_prints_usage() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not installed")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 2
    assert "quickstart.sh data" in result.stderr


def render(*files: Path, profiles: tuple[str, ...] = ("local-models",)) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not installed")
    args = [docker, "compose", "--env-file", os.devnull, "--project-name", "lorebase-test"]
    for file in files:
        args += ["-f", str(file)]
    kept = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME"} or k.startswith("DOCKER_")}
    env = {
        **kept,
        "POSTGRES_PASSWORD": "owner-secret",
        "LOREBASE_APP_PASSWORD": "app-secret",
        "LOREBASE_WEBHOOK_KEY": "w" * 64,
    }
    result = subprocess.run(
        [*args, *(x for p in profiles for x in ("--profile", p)), "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 and "unknown" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    rendered: dict[str, Any] = json.loads(result.stdout)
    return rendered


def test_hosted_model_overlay_reads_the_key_from_a_secret_file() -> None:
    config = render(COMPOSE_FILE, HOSTED_FILE)
    services = config["services"]
    for name in ("scribe", "inference-check"):
        service = services[name]
        assert service["environment"]["LLM_API_KEY"] == "secret://file//run/secrets/llm_api_key"
        assert [s["source"] for s in service["secrets"]] == ["llm_api_key"]
        # The rest of the model settings still come from the main file.
        assert service["environment"]["LLM_BASE_URL"] == "http://ollama:11434/v1"
    assert config["secrets"]["llm_api_key"]["file"] == str(ROOT / "secrets" / "llm_api_key")


def test_scribe_and_guide_use_the_embedding_image() -> None:
    services = render(COMPOSE_FILE, profiles=("assistant",))["services"]
    for name in ("scribe", "guide"):
        service = services[name]
        assert service["build"]["target"] == "backend-ml"
        assert service["image"] == "lorebase-ml:dev"
    assert "target" not in services["keeper"]["build"]
    assert services["keeper"]["image"] == "lorebase:dev"


def test_assistant_overlay_points_the_web_app_at_guide() -> None:
    config = render(
        COMPOSE_FILE, ROOT / "deploy" / "compose" / "assistant.yaml", profiles=("assistant",)
    )
    env = config["services"]
    assert env["web"]["environment"]["VITE_GUIDE_URL"] == "/guide"
    anonymous = env["guide"]["environment"]["LOREBASE__GUIDE__HTTP__ALLOW_ANONYMOUS"]
    assert anonymous == "true"


def test_model_services_reach_a_model_server_on_the_host() -> None:
    services = render(COMPOSE_FILE)["services"]
    for name in ("scribe", "inference-check"):
        hosts = [h.replace(":", "=", 1) for h in services[name]["extra_hosts"]]
        assert "host.docker.internal=host-gateway" in hosts
        assert services[name]["environment"]["LLM_API_KEY"] == ""
        assert "secrets" not in services[name]

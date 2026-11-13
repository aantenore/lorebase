"""The quickstart helpers: `scripts/quickstart.sh env`, the checks and the lines `up` prints
(with a `docker` that only echoes its arguments), and the hosted model compose overlay.

The stack commands of the script (up, data, down, clean) need Docker and a running Keeper for
the rest; they are exercised by the fresh-clone check in docs/validation.md, not here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "quickstart.sh"
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
HOSTED_FILE = ROOT / "deploy" / "compose" / "hosted-model.yaml"
PASSWORDS = ("POSTGRES_PASSWORD", "LOREBASE_APP_PASSWORD")
GENERATED = (*PASSWORDS, "LOREBASE_WEBHOOK_KEY", "HERALD_DOMAIN")
# Host port variables of the services `up` can start, in the order the script probes them.
PORT_VARIABLES = (
    "POSTGRES_HOST_PORT",
    "KEEPER_HOST_PORT",
    "LOREBASE_WEB_PORT",
    "GUIDE_HOST_PORT",
    "HERALD_HOST_PORT",
)


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
    # The sample Domain for the mcp profile, set where .env.example documents it.
    assert got["HERALD_DOMAIN"] == "supply"
    lines = env_file.read_text().splitlines()
    assert "# HERALD_DOMAIN=supply" not in lines
    assert lines.index("HERALD_DOMAIN=supply") < lines.index("# HERALD_BRANCH=")


def test_env_keeps_set_values_and_fills_placeholders(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=change-me\nLOREBASE_APP_PASSWORD=mine\nKEEPER_HOST_PORT=18080\n"
        "HERALD_DOMAIN=mine-too\n"
    )
    run_env(env_file)
    first = values(env_file)
    assert re.fullmatch(r"[0-9a-f]{48}", first["POSTGRES_PASSWORD"])
    assert first["LOREBASE_APP_PASSWORD"] == "mine"
    assert first["KEEPER_HOST_PORT"] == "18080"
    assert first["HERALD_DOMAIN"] == "mine-too"
    # A file from before the webhook key gets one, appended.
    assert re.fullmatch(r"[0-9a-f]{64}", first["LOREBASE_WEBHOOK_KEY"])

    run_env(env_file)
    assert values(env_file) == first


def test_env_appends_the_sample_domain_to_a_file_without_it(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=x\nLOREBASE_APP_PASSWORD=y\nLOREBASE_WEBHOOK_KEY=z\n")
    result = run_env(env_file)
    assert values(env_file)["HERALD_DOMAIN"] == "supply"
    assert "set HERALD_DOMAIN=supply" in result.stderr


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


def test_makefile_prints_the_shell_exports_on_their_own() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "shell-env:" in makefile
    assert "\n\t@scripts/quickstart.sh shell-env\n" in makefile  # stdout holds the line only
    assert " shell-env " in makefile.split(".PHONY:")[1].split("\n\n")[0].replace("\\\n", " ")


def test_shell_env_prints_the_export_line_for_the_configured_port(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEEPER_HOST_PORT=18580\n")
    result = run_script("shell-env", env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "export LOREBASE_URL=http://127.0.0.1:18580 LOREBASE_DOMAIN=supply\n"
    assert run_script("shell-env", env_file=tmp_path / "missing.env").returncode == 1


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


def run_with_docker(
    tmp_path: Path, *args: str, env_file: Path, **extra: str
) -> subprocess.CompletedProcess[str]:
    """Run the script with a `docker` on PATH that prints its arguments, one per line."""
    if shutil.which("bash") is None:
        pytest.skip("bash not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LOREBASE_ENV_FILE": str(env_file),
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def compose_files(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]


def test_compose_overlays_from_the_env_file_follow_the_stack_files(tmp_path: Path) -> None:
    overlay = tmp_path / "my-model.yaml"
    overlay.write_text("services: {}\n")
    env_file = tmp_path / ".env"
    env_file.write_text(f'COMPOSE_OVERLAYS="deploy/compose/hosted-model.yaml {overlay}"\n')
    result = run_with_docker(tmp_path, "down", env_file=env_file, PROFILES="assistant")
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert compose_files(args) == [
        str(COMPOSE_FILE),
        str(COMPOSE_FILE.parent / "assistant.yaml"),
        "deploy/compose/hosted-model.yaml",
        str(overlay),
    ]
    assert args[-2:] == ["down", "--remove-orphans"]


def test_compose_overlays_from_the_environment_win_and_logs_use_them(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_OVERLAYS=missing.yaml\n")
    result = run_with_docker(
        tmp_path,
        "logs",
        "scribe",
        env_file=env_file,
        COMPOSE_OVERLAYS="deploy/compose/hosted-model.yaml",
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert compose_files(args) == [str(COMPOSE_FILE), "deploy/compose/hosted-model.yaml"]
    assert args[-5:] == ["logs", "--follow", "--tail", "200", "scribe"]


def test_a_missing_compose_overlay_stops_before_compose_runs(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_OVERLAYS=secrets/nowhere.yaml\n")
    result = run_with_docker(tmp_path, "down", env_file=env_file)
    assert result.returncode == 2
    assert "secrets/nowhere.yaml, which does not exist" in result.stderr
    assert result.stdout == ""


def test_makefile_hands_compose_overlays_to_the_script() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "export COMPOSE_OVERLAYS" in makefile
    assert "scripts/quickstart.sh logs $(SERVICES)" in makefile
    assert "COMPOSE_OVERLAYS" in values(ROOT / ".env.example")


# `up` before compose: the port and exposure checks, and the lines it prints afterwards -------


def free_ports(count: int) -> list[int]:
    """Ports nothing listens on right now (bound and released one after the other)."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def stack_env(tmp_path: Path, **extra: str) -> Path:
    """An environment file whose published ports are all free, plus `extra`."""
    settings = dict(zip(PORT_VARIABLES, map(str, free_ports(len(PORT_VARIABLES))), strict=True))
    settings.update(extra)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in settings.items()))
    return env_file


@pytest.fixture
def listener() -> Iterator[int]:
    """A port something listens on, on 127.0.0.1."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def compose_up(result: subprocess.CompletedProcess[str]) -> list[str]:
    """The arguments of the `compose up` call the echoing docker saw, if any (the script sends
    that call's output to standard error, before the lines it prints itself)."""
    lines = result.stderr.splitlines()
    if "up" not in lines:
        return []
    args = lines[lines.index("up") :]
    ends = [i for i, line in enumerate(args) if line.startswith("[quickstart]")]
    return args[: ends[0]] if ends else args


def test_up_stops_before_compose_when_a_published_port_is_taken(
    tmp_path: Path, listener: int
) -> None:
    env_file = stack_env(tmp_path, GUIDE_HOST_PORT=str(listener))
    result = run_with_docker(tmp_path, "up", env_file=env_file, PROFILES="assistant")
    assert result.returncode == 1
    assert f"port {listener} on 127.0.0.1 (GUIDE_HOST_PORT) is already in use" in result.stderr
    assert f"set GUIDE_HOST_PORT in {env_file}" in result.stderr
    assert compose_up(result) == []
    # Without the profile that publishes it, the port is nobody's business.
    result = run_with_docker(tmp_path, "up", env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert compose_up(result)[:4] == ["up", "-d", "--build", "--wait"]


def test_up_pulls_the_release_images_when_asked(tmp_path: Path) -> None:
    env_file = stack_env(tmp_path)
    result = run_with_docker(
        tmp_path, "up", env_file=env_file, IMAGE_SOURCE="release", RELEASE_TAG="9.9.9"
    )
    assert result.returncode == 0, result.stderr
    assert compose_up(result)[:4] == ["up", "-d", "--no-build", "--wait"]
    assert "ghcr.io/aantenore/lorebase*:9.9.9" in result.stderr
    result = run_with_docker(tmp_path, "up", env_file=env_file, IMAGE_SOURCE="registry")
    assert result.returncode == 2
    assert compose_up(result) == []


def test_up_probes_the_configured_bind_address(tmp_path: Path, listener: int) -> None:
    # Published on every address, a port taken on 127.0.0.1 is taken (the exposure check is
    # opted out of first, or it would stop the run before the probe).
    env_file = stack_env(
        tmp_path,
        KEEPER_HOST_PORT=str(listener),
        KEEPER_BIND="0.0.0.0",
        KEEPER_PUBLISH_INSECURE="true",
    )
    result = run_with_docker(tmp_path, "up", env_file=env_file)
    assert result.returncode == 1
    assert f"port {listener} on 0.0.0.0 (KEEPER_HOST_PORT) is already in use" in result.stderr


def test_up_prints_the_export_line_for_the_configured_port(tmp_path: Path) -> None:
    env_file = stack_env(tmp_path)
    port = values(env_file)["KEEPER_HOST_PORT"]
    result = run_with_docker(tmp_path, "up", env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert compose_up(result) == [
        "up",
        "-d",
        "--build",
        "--wait",
        "postgres",
        "migrate",
        "keeper",
        "web",
    ]
    assert f"Keeper is ready at http://127.0.0.1:{port}" in result.stderr
    assert f"export LOREBASE_URL=http://127.0.0.1:{port} LOREBASE_DOMAIN=supply" in result.stderr
    assert 'eval "$(make shell-env)"' in result.stderr
    assert (
        f'"env": {{"LOREBASE_URL": "http://127.0.0.1:{port}", "LOREBASE_DOMAIN": "supply"}}'
        in result.stderr
    )


def test_up_with_the_mcp_profile_names_the_streamable_http_endpoint(tmp_path: Path) -> None:
    env_file = stack_env(tmp_path)
    port = values(env_file)["HERALD_HOST_PORT"]
    result = run_with_docker(tmp_path, "up", env_file=env_file, PROFILES="mcp")
    assert result.returncode == 0, result.stderr
    assert compose_up(result)[-1] == "herald"
    assert f"http://127.0.0.1:{port}/mcp" in result.stderr


def test_up_refuses_to_publish_an_unauthenticated_keeper_beyond_loopback(
    tmp_path: Path,
) -> None:
    # The web service proxies /api/ to Keeper, so its bind address counts as well.
    env_file = stack_env(tmp_path, WEB_BIND="0.0.0.0")
    result = run_with_docker(tmp_path, "up", env_file=env_file)
    assert result.returncode == 1
    assert "refusing to start: WEB_BIND=0.0.0.0 publishes Keeper beyond this machine" in (
        result.stderr
    )
    assert "administrator of every Domain" in result.stderr
    assert "KEEPER_PUBLISH_INSECURE=true" in result.stderr
    assert compose_up(result) == []
    # The binds of the profiles count only with the profile.
    env_file = stack_env(tmp_path, HERALD_BIND="0.0.0.0")
    assert run_with_docker(tmp_path, "up", env_file=env_file).returncode == 0
    result = run_with_docker(tmp_path, "up", env_file=env_file, PROFILES="mcp")
    assert result.returncode == 1
    assert "HERALD_BIND=0.0.0.0" in result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        {"KEEPER_PUBLISH_INSECURE": "true"},
        {"LOREBASE__KEEPER__AUTH__MODE": "api_key"},
        {"KEEPER_ALLOW_INSECURE": "false"},
    ],
)
def test_up_publishes_beyond_loopback_with_authentication_or_an_explicit_opt_in(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    env_file = stack_env(tmp_path, KEEPER_BIND="0.0.0.0", **extra)
    result = run_with_docker(tmp_path, "up", env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert compose_up(result)[:1] == ["up"]
    # Only the opt-in is worth a warning: the other two run an authenticating Keeper.
    assert ("warning: Keeper takes every request" in result.stderr) == (
        "KEEPER_PUBLISH_INSECURE" in extra
    )


def test_the_opt_in_is_read_from_the_environment_too(tmp_path: Path) -> None:
    env_file = stack_env(tmp_path, KEEPER_BIND="0.0.0.0")
    result = run_with_docker(tmp_path, "up", env_file=env_file, KEEPER_PUBLISH_INSECURE="true")
    assert result.returncode == 0, result.stderr
    assert "KEEPER_PUBLISH_INSECURE=true" in result.stderr


def test_shell_variables_win_over_the_env_file_in_the_exposure_check(tmp_path: Path) -> None:
    # docker compose gives the shell precedence over --env-file, so the check does the same.
    env_file = stack_env(
        tmp_path, KEEPER_BIND="127.0.0.1", WEB_BIND="localhost", KEEPER_ALLOW_INSECURE="false"
    )
    result = run_with_docker(
        tmp_path, "up", env_file=env_file, KEEPER_BIND="0.0.0.0", KEEPER_ALLOW_INSECURE="true"
    )
    assert result.returncode == 1
    assert "refusing to start: KEEPER_BIND=0.0.0.0 publishes Keeper beyond this machine" in (
        result.stderr
    )
    assert "administrator of every Domain" in result.stderr
    assert compose_up(result) == []
    # An opt-in in the env file loses to one turned off in the shell.
    env_file = stack_env(tmp_path, KEEPER_PUBLISH_INSECURE="true")
    result = run_with_docker(
        tmp_path, "up", env_file=env_file, KEEPER_BIND="0.0.0.0", KEEPER_PUBLISH_INSECURE="false"
    )
    assert result.returncode == 1
    assert compose_up(result) == []
    # A loopback address in the shell wins over an exposed one in the env file.
    env_file = stack_env(tmp_path, KEEPER_BIND="0.0.0.0")
    for address in ("127.0.0.1", "localhost", "::1", "[::1]"):
        result = run_with_docker(tmp_path, "up", env_file=env_file, KEEPER_BIND=address)
        assert "refusing to start" not in result.stderr, address


def test_the_opt_in_in_the_shell_allows_a_bind_set_in_the_shell(tmp_path: Path) -> None:
    env_file = stack_env(tmp_path, KEEPER_BIND="127.0.0.1")
    result = run_with_docker(
        tmp_path,
        "up",
        env_file=env_file,
        KEEPER_BIND="0.0.0.0",
        KEEPER_ALLOW_INSECURE="true",
        KEEPER_PUBLISH_INSECURE="true",
    )
    assert result.returncode == 0, result.stderr
    assert "warning: Keeper takes every request" in result.stderr
    assert "KEEPER_BIND=0.0.0.0" in result.stderr
    assert compose_up(result)[:1] == ["up"]


def test_the_port_check_reads_the_shell_first(tmp_path: Path, listener: int) -> None:
    env_file = stack_env(tmp_path)
    result = run_with_docker(tmp_path, "up", env_file=env_file, KEEPER_HOST_PORT=str(listener))
    assert result.returncode == 1
    assert f"port {listener} on 127.0.0.1 (KEEPER_HOST_PORT) is already in use" in result.stderr
    assert compose_up(result) == []

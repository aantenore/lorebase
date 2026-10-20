"""CLI settings: command line, then environment, then the config file, then defaults.

The config file is TOML, by default `$XDG_CONFIG_HOME/lorebase/config.toml` (falling back to
`~/.config/lorebase/config.toml`); `--config` or `LOREBASE_CONFIG` names another one:

    profile = "local"              # profile used when none is asked for

    [profiles.local]
    url = "http://localhost:8080"
    api_key = "lbk_..."            # or token = "<OIDC access token>"
    domain = "supply"              # default for --domain
    branch = "work"                # default for --branch

    [import]
    batch_size = 1000              # rows per request

    [output]
    list_limit = 50                # items printed by list commands without --all
    max_cell_width = 60            # longer table cells are cut

Keys outside `[profiles.*]` apply to every profile. Credentials are not accepted as command
line options (they would end up in shell history): use the environment or the config file,
which should be readable by its owner only.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lorebase_cli.failures import CliError

ENV_CONFIG = "LOREBASE_CONFIG"
ENV_PROFILE = "LOREBASE_PROFILE"
ENV_DOMAIN = "LOREBASE_DOMAIN"
ENV_BRANCH = "LOREBASE_BRANCH"
ENV_BATCH_SIZE = "LOREBASE_BATCH_SIZE"
ENV_LIST_LIMIT = "LOREBASE_LIST_LIMIT"

DEFAULT_BATCH_SIZE = 1000
DEFAULT_LIST_LIMIT = 50
DEFAULT_MAX_CELL_WIDTH = 60
CONFIG_DIR = "lorebase"
CONFIG_FILE = "config.toml"


@dataclass(frozen=True)
class CliSettings:
    """Everything the commands need from configuration."""

    url: str | None = None
    api_prefix: str | None = None
    api_key: str | None = None
    token: str | None = None
    timeout: float | None = None
    domain: str | None = None
    branch: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    list_limit: int = DEFAULT_LIST_LIMIT
    max_cell_width: int = DEFAULT_MAX_CELL_WIDTH
    profile: str | None = None
    config_path: Path | None = None


def default_config_path(env: Mapping[str, str]) -> Path:
    """Where the config file is looked for when none is named."""
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / CONFIG_DIR / CONFIG_FILE


def _read(path: Path, *, explicit: bool) -> dict[str, Any]:
    if not path.exists():
        if explicit:
            raise CliError(f"config file not found: {path}")
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CliError(f"cannot read config file {path}: {error}") from error


def _positive(value: object, name: str) -> int:
    try:
        number = int(str(value))
    except ValueError:
        number = 0
    if number < 1:
        raise CliError(f"{name} must be a positive integer, got {value!r}")
    return number


def _number(value: object, name: str) -> float:
    try:
        return float(str(value))
    except ValueError:
        raise CliError(f"{name} must be a number, got {value!r}") from None


def _text(table: Mapping[str, Any], key: str) -> str | None:
    value = table.get(key)
    return None if value is None else str(value)


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    profile: str | None = None,
    url: str | None = None,
    warn: Any = None,
) -> CliSettings:
    """Merge the command line values given, the environment and the config file.

    `warn` is called with a message when the config file holds credentials and other users
    can read it.
    """
    environment = os.environ if env is None else env
    named = config_path or (Path(environment[ENV_CONFIG]) if environment.get(ENV_CONFIG) else None)
    path = named or default_config_path(environment)
    document = _read(path, explicit=named is not None)

    chosen = profile or environment.get(ENV_PROFILE) or document.get("profile")
    profiles = document.get("profiles", {})
    if chosen is not None and chosen not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise CliError(f"profile {chosen!r} is not in {path} (profiles: {known})")
    layer: dict[str, Any] = {**document, **(profiles.get(chosen, {}) if chosen else {})}
    importing = {**document.get("import", {}), **layer.get("import", {})}
    output = {**document.get("output", {}), **layer.get("output", {})}

    def pick(env_name: str, key: str) -> str | None:
        return environment.get(env_name) or _text(layer, key)

    from_file = (layer.get("api_key") and not environment.get("LOREBASE_API_KEY")) or (
        layer.get("token") and not environment.get("LOREBASE_TOKEN")
    )
    shared = stat.S_IRWXG | stat.S_IRWXO
    if warn and from_file and path.exists() and path.stat().st_mode & shared:
        warn(f"{path} holds credentials and other users can read it; run: chmod 600 {path}")

    settings = CliSettings(
        url=url or pick("LOREBASE_URL", "url"),
        api_prefix=pick("LOREBASE_API_PREFIX", "api_prefix"),
        api_key=pick("LOREBASE_API_KEY", "api_key"),
        token=pick("LOREBASE_TOKEN", "token"),
        domain=pick(ENV_DOMAIN, "domain"),
        branch=pick(ENV_BRANCH, "branch"),
        profile=chosen,
        config_path=path if path.exists() else None,
    )
    timeout = pick("LOREBASE_TIMEOUT", "timeout")
    if timeout is not None:
        settings = replace(settings, timeout=_number(timeout, "timeout"))
    size = environment.get(ENV_BATCH_SIZE) or importing.get("batch_size")
    if size is not None:
        settings = replace(settings, batch_size=_positive(size, "batch_size"))
    limit = environment.get(ENV_LIST_LIMIT) or output.get("list_limit")
    if limit is not None:
        settings = replace(settings, list_limit=_positive(limit, "list_limit"))
    width = output.get("max_cell_width")
    if width is not None:
        settings = replace(settings, max_cell_width=_positive(width, "max_cell_width"))
    return settings

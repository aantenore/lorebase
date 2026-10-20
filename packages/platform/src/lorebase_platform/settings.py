"""Layered settings: code defaults, then a YAML file, then environment variables.

Sources, lowest to highest precedence:

1. defaults declared on the section models;
2. the YAML file named by `LOREBASE_CONFIG` (optional);
3. environment variables `LOREBASE__SECTION__KEY` (nested keys use further `__`), then the
   short aliases a section registers (for example `LLM_BASE_URL`).

Sections owned by this package (`telemetry`, `logging`) are typed fields of `Settings`. Other
packages declare their section as a model and register it with `register_section`; they read it
with `Settings.section`. `load_settings` validates every registered section, so invalid
configuration stops the process at startup with a message naming each key and the reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG_ENV_VAR = "LOREBASE_CONFIG"
ENV_PREFIX = "LOREBASE__"
ENV_DELIMITER = "__"

SectionT = TypeVar("SectionT", bound=BaseModel)


class ConfigError(Exception):
    """Invalid configuration. The message names every offending key and the reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))

    @classmethod
    def from_validation(cls, exc: ValidationError, prefix: str = "") -> ConfigError:
        """Build the error from pydantic details, never echoing input values."""
        return cls(_problems(exc, prefix))


def _problems(exc: ValidationError, prefix: str = "") -> list[str]:
    problems = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        key = ".".join(str(part) for part in (*([prefix] if prefix else []), *error["loc"]))
        problems.append(f"{key or '<root>'}: {error['msg']}")
    return problems


class SectionModel(BaseModel):
    """Base class for settings sections: immutable, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TelemetrySettings(SectionModel):
    """OpenTelemetry export. Disabled unless `otlp_endpoint` is set."""

    otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP gRPC collector endpoint, e.g. http://otel-collector:4317. "
        "Unset or empty disables traces and metrics.",
    )
    service_name: str = Field(default="lorebase", description="Value of `service.name`.")
    service_namespace: str = Field(default="lorebase", description="Value of `service.namespace`.")
    sample_ratio: float = Field(
        default=0.1, ge=0, le=1, description="Fraction of root traces that are sampled."
    )
    metric_export_interval_s: float = Field(
        default=60, gt=0, description="Seconds between metric exports."
    )

    @field_validator("otlp_endpoint")
    @classmethod
    def _empty_means_unset(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


class LoggingSettings(SectionModel):
    """Structured logging and redaction."""

    level: str = Field(default="INFO", description="Root log level name.")
    format: str = Field(default="json", pattern="^(json|text)$", description="json or text.")
    redact_keys: tuple[str, ...] = Field(
        default=("password", "secret", "token", "api_key", "authorization", "credential"),
        description="Field names whose values are masked. A name matches a whole "
        "underscore-separated key or a part of it (access_token matches token).",
    )
    redact_min_length: int = Field(
        default=4,
        ge=1,
        description="Secret values shorter than this are not masked inside free text.",
    )

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        name = value.upper()
        if name not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG")
        return name


@dataclass(frozen=True)
class SectionSpec:
    """A settings section contributed by a package."""

    name: str
    model: type[BaseModel]
    env_aliases: Mapping[str, str] = field(default_factory=dict)
    """Environment variable name -> dotted key inside the section, e.g. LLM_MODEL -> llm.model."""


_SECTIONS: dict[str, SectionSpec] = {}


def register_section(
    name: str, model: type[BaseModel], env_aliases: Mapping[str, str] | None = None
) -> None:
    """Declare a settings section; repeating the same declaration is harmless."""
    if name in Settings.model_fields:
        raise ValueError(f"section {name!r} is owned by lorebase_platform")
    spec = SectionSpec(name, model, dict(env_aliases or {}))
    existing = _SECTIONS.get(name)
    if existing is not None and existing != spec:
        raise ValueError(f"section {name!r} is already registered with a different model")
    _SECTIONS[name] = spec


def registered_sections() -> Mapping[str, SectionSpec]:
    """Sections registered so far."""
    return dict(_SECTIONS)


def _parse_value(raw: str) -> Any:
    stripped = raw.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return raw
    return raw


def _assign(tree: dict[str, Any], path: list[str], value: Any, origin: str) -> None:
    node = tree
    for part in path[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigError([f"{origin}: conflicts with a value set for {part!r}"])
        node = child
    if isinstance(node.get(path[-1]), dict):
        raise ConfigError([f"{origin}: conflicts with nested keys under {path[-1]!r}"])
    node[path[-1]] = value


def environment_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Nested mapping from `LOREBASE__A__B` variables and registered section aliases."""
    tree: dict[str, Any] = {}
    for name in sorted(environ):
        if name.startswith(ENV_PREFIX):
            path = [p.lower() for p in name[len(ENV_PREFIX) :].split(ENV_DELIMITER)]
            if all(path):
                _assign(tree, path, _parse_value(environ[name]), name)
    for spec in _SECTIONS.values():
        for alias, dotted in sorted(spec.env_aliases.items()):
            value = environ.get(alias)
            path = [spec.name, *dotted.split(".")]
            if value and _lookup(tree, path) is None:
                _assign(tree, path, value, alias)
    return tree


def _lookup(tree: Mapping[str, Any], path: list[str]) -> Any:
    node: Any = tree
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


class _EnvSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return environment_overrides(os.environ)


class _YamlSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        location = os.environ.get(CONFIG_ENV_VAR)
        if not location:
            return {}
        path = Path(location)
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(
                [f"{CONFIG_ENV_VAR}: cannot read {location!r}: {exc.strerror}"]
            ) from None
        except yaml.YAMLError as exc:
            raise ConfigError([f"{location}: invalid YAML: {exc}"]) from None
        if content is None:
            return {}
        if not isinstance(content, dict):
            raise ConfigError([f"{location}: top level must be a mapping"])
        return {str(key): value for key, value in content.items()}


class Settings(BaseSettings):
    """Process-wide settings. Build with `load_settings`."""

    model_config = SettingsConfigDict(extra="allow", frozen=True)

    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _EnvSource(settings_cls), _YamlSource(settings_cls))

    def section(self, name: str, model: type[SectionT]) -> SectionT:
        """Validated settings section declared by another package."""
        raw = (self.model_extra or {}).get(name) or {}
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError.from_validation(exc, prefix=name) from None


def load_settings() -> Settings:
    """Load and fully validate settings; raise `ConfigError` listing every problem."""
    try:
        settings = Settings()
    except ValidationError as exc:
        raise ConfigError.from_validation(exc) from None
    problems: list[str] = []
    for spec in _SECTIONS.values():
        try:
            settings.section(spec.name, spec.model)
        except ConfigError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ConfigError(problems)
    return settings

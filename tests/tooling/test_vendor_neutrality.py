"""No package depends on a model vendor SDK, and provider names stay in the registry."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR_SDKS = frozenset(
    {
        "openai",
        "anthropic",
        "litellm",
        "cohere",
        "mistralai",
        "google-generativeai",
        "google-genai",
        "boto3",
        "langchain",
        "langchain-openai",
        "ollama",
    }
)
PROVIDER_NAMES = re.compile(r"openai|fastembed|gliner|glirel|ollama|litellm", re.IGNORECASE)
PROVIDER_AWARE_MODULES = {"registry.py", "settings.py"}


def pyprojects() -> list[Path]:
    return [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]


def dependency_names(path: Path) -> set[str]:
    data = tomllib.loads(path.read_text())
    requirements = list(data["project"].get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))
    return {re.split(r"[<>=!~\[; ]", item, maxsplit=1)[0].lower() for item in requirements}


@pytest.mark.parametrize("path", pyprojects(), ids=lambda p: p.parent.name)
def test_no_vendor_sdk_dependency(path: Path) -> None:
    assert dependency_names(path) & VENDOR_SDKS == set()


def test_provider_names_appear_only_in_registry_and_settings() -> None:
    sources = (ROOT / "packages" / "inference" / "src" / "lorebase_inference").glob("*.py")
    offenders = [
        p.name
        for p in sources
        if p.name not in PROVIDER_AWARE_MODULES and PROVIDER_NAMES.search(p.read_text())
    ]
    assert offenders == []

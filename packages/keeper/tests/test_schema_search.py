"""The search block of `GET /schema`: modes and the embedding model, from settings."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import yaml

from lorebase_core.model import load_schema
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.settings import SearchSettings
from lorebase_keeper.routes.schema import _search
from lorebase_store.settings import EmbeddingStoreSettings, StoreSettings

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"


def runtime(model: str, dims: int) -> ReadRuntime:
    stand_in = SimpleNamespace(
        search=SearchSettings.model_validate({"semantic": {"model": model}}),
        store_settings=StoreSettings(embeddings=EmbeddingStoreSettings(dims=dims)),
    )
    return cast(ReadRuntime, stand_in)


def supply(template: str | None) -> Any:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    if template is not None:
        document["kinds"]["Supplier"]["search"] = {"embedding_template": template}
    return load_schema(yaml.safe_dump(document))


def test_schema_without_embedding_template_is_lexical_only() -> None:
    out = _search(supply(None), runtime("acme/embed", 8))
    assert out.modes == ["lexical"]
    assert out.embedding is None


def test_embedding_model_and_dimension_come_from_settings() -> None:
    out = _search(supply("{legal_name}"), runtime("acme/embed", 8))
    assert out.modes == ["lexical", "semantic", "hybrid"]
    assert out.embedding is not None
    assert (out.embedding.model, out.embedding.dims) == ("acme/embed", 8)

"""Request bodies of the read API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lorebase_core.query import Predicate, Query

AT = (
    "Revision id or tag to read at (time travel): key-ordered listings read the tree, other "
    "reads a snapshot of the Revision."
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryRequest(Query):
    """A DSL query (design 8.2) and where to read it."""

    branch: str | None = Field(default=None, description="Default: the Domain's default branch.")
    at: str | None = Field(default=None, description=AT)


class RecordAddress(_Body):
    kind: str = Field(min_length=1)
    key: str = Field(min_length=1)


class TraverseStep(_Body):
    relation: str = Field(min_length=1, description="Forward or inverse Relation api name.")
    where: Predicate | None = Field(default=None, description="Filter on the Records reached.")
    limit: int | None = Field(default=None, ge=1, description="Records reached by this step.")


class TraverseRequest(_Body):
    """Follow a path of Relations from one Record (design 8.3)."""

    branch: str | None = None
    at: str | None = Field(default=None, description=AT)
    start: RecordAddress
    path: list[TraverseStep] = Field(min_length=1)


class NeighborhoodRequest(_Body):
    """Breadth-first expansion from seed Records, bounded by depth and caps (design 8.3)."""

    branch: str | None = None
    at: str | None = Field(default=None, description=AT)
    seeds: list[RecordAddress] = Field(min_length=1)
    depth: int = Field(default=1, ge=1)
    relations: list[str] | None = Field(
        default=None,
        description="Relation api names to follow; a forward name follows Links forward, an "
        "inverse name backward. Default: every Relation in both directions.",
    )
    max_nodes: int | None = Field(default=None, ge=1)
    max_edges: int | None = Field(default=None, ge=1)
    values: bool = Field(
        default=True,
        description="false: nodes are Record headers (kind, key, title) and edges Link "
        "endpoints (relation, from, to), without values; for callers that need the structure "
        "only.",
    )


SearchMode = Literal["lexical", "semantic", "hybrid"]


class SearchTuning(_Body):
    """Ranking knobs of one search in place of the `search` settings (to compare parameter
    sets). Unset knobs keep their configured value; `candidates` and `typo_candidates` may only
    be lowered and `trigram_threshold` only raised."""

    lexical_weight: float | None = Field(default=None, ge=0, le=100)
    semantic_weight: float | None = Field(default=None, ge=0, le=100)
    rrf_k: int | None = Field(default=None, ge=1, le=10_000)
    trigram_threshold: float | None = Field(default=None, ge=0.1, le=1)
    trigram_weight: float | None = Field(default=None, ge=0, le=100)
    typo_candidates: int | None = Field(default=None, ge=0)
    candidates: int | None = Field(default=None, ge=1)


class SearchRequest(_Body):
    """Full-text, vector or hybrid search (design 8.4). Vectors come from the caller."""

    branch: str | None = None
    at: str | None = Field(default=None, description=AT)
    q: str | None = Field(default=None, min_length=1, description="Text query (web search syntax).")
    kinds: list[str] | None = Field(default=None, description="Kind api names; default: all.")
    traits: list[str] | None = Field(
        default=None,
        description="Trait api names: each stands for the Kinds implementing it that the "
        "caller may read. With `kinds`, the union is searched.",
    )
    lens: str | None = Field(
        default=None,
        description="Lens api name: its Kinds and Traits are searched when the request names "
        "neither `kinds` nor `traits`.",
    )
    where: Predicate | None = Field(
        default=None, description="DSL filter; needs exactly one Kind in the searched Kinds."
    )
    mode: SearchMode | None = Field(
        default=None,
        description="Default: hybrid with both `q` and `query_vector`, otherwise the one given.",
    )
    query_vector: list[float] | None = Field(
        default=None, description="Query embedding of the configured model and dimension."
    )
    limit: int | None = Field(default=None, ge=1)
    tuning: SearchTuning | None = Field(
        default=None, description="Ranking knobs in place of the `search` settings."
    )

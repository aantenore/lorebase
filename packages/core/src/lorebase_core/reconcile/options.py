"""Options of the reconciliation pipeline (design 10.6), with documented defaults.

Scribe exposes them as ``scribe.reconciliation`` (see ``config/lorebase.example.yaml``); a Domain
overrides them in its ``reconciliation`` setting. Every threshold of ``lorebase_core.reconcile``
is read from here; nothing is hard-coded in the stages.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lorebase_core.types import AttributeType, TypeSpec, decode_value, encode_value

_DURATION = TypeSpec.of(AttributeType.DURATION)


class Stage(StrEnum):
    """Detection stages, cheapest first; the pipeline runs them in this order."""

    IDENTITY = "identity"
    VALUES = "values"
    NORMALIZE = "normalize"
    SEMANTIC = "semantic"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


MergeCheck = Literal["off", "warn", "block"]
"""Mode of the `reconciliation` Proposal check (design 10.3, 10.6)."""


class _Options(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TriggerOptions(_Options):
    """Which writes are checked as they happen (the sweep runs whatever these say)."""

    extraction: bool = Field(
        default=True, description="Check extraction value items while they are assembled."
    )
    source_sync: bool = Field(
        default=True, description="Check the changes of a Source sync before it is merged."
    )
    document_versions: bool = Field(
        default=True,
        description="Check the evidence quotes of values when a new version of their document "
        "arrives or the document is retired (job document_version_check).",
    )


class DuplicateOptions(_Options):
    """Stage ``identity``: Records of one Kind that probably describe the same entity."""

    trigram_threshold: float = Field(
        default=0.8,
        gt=0,
        le=1,
        description="Least trigram similarity of two titles for a duplicate candidate.",
    )
    max_pairs_per_kind: int = Field(
        default=1000, ge=1, description="Duplicate pairs examined per Kind and pass."
    )


class NormalizeOptions(_Options):
    """Stage ``normalize`` on ``string`` and ``text`` values: it filters and ranks candidates
    and never changes a stored value."""

    units_file: str | None = Field(
        default=None,
        description="YAML file of units and factors used instead of the table shipped with "
        "lorebase_core (same format: dimensions mapping units to factor and aliases).",
    )
    numeric_tolerance: float = Field(
        default=0.0,
        ge=0,
        description="Relative difference under which two numbers are equal; 0 compares exactly.",
    )
    max_quantity_length: int = Field(
        default=64,
        ge=1,
        le=1024,
        description="Longer values are not read as quantities (a number and a unit).",
    )
    negation_words: tuple[str, ...] = Field(
        default=("not", "no", "never", "without"),
        description="Words whose removal makes two values equal: such pairs are kept and "
        "marked negation, which ranks them first.",
    )

    @field_validator("negation_words")
    @classmethod
    def _folded(cls, words: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(w.strip().casefold() for w in words if w.strip()))


class SemanticOptions(_Options):
    """Stage ``semantic`` (optional): embeddings of competing ``text`` values."""

    enabled: bool = Field(
        default=False, description="Compare embeddings; needs an embedding provider (9.4)."
    )
    equivalence_threshold: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Similarity above which near-paraphrases are not raised; null raises them "
        "all, marked equivalent and ranked last.",
    )
    equivalent_similarity: float = Field(
        default=0.9,
        gt=0,
        le=1,
        description="Least similarity of competing text values marked equivalent (near-"
        "paraphrases, ranked last); they are still raised unless above equivalence_threshold.",
    )
    duplicate_threshold: float = Field(
        default=0.92,
        gt=0,
        le=1,
        description="Least similarity of two stored Record vectors for a duplicate candidate.",
    )
    judge: bool = Field(
        default=False,
        description="Ask the chat model whether two texts contradict; adds a label and a score.",
    )


class StaleOptions(_Options):
    """``stale`` candidates: values older than a maximum age, and Records an ``incremental``
    Source has not seen within its ``stale_after``."""

    max_age: str | None = Field(
        default=None,
        description="ISO 8601 duration (days to seconds, e.g. P30D); null turns the age check "
        "off. Kinds and Attributes override it in the Domain settings.",
    )
    source_unseen: bool = Field(
        default=True,
        description="Raise source_unseen candidates for Records an incremental Source has not "
        "read within its stale_after (a Source setting; null there turns it off per Source).",
    )

    @field_validator("max_age")
    @classmethod
    def _duration(cls, value: str | None) -> str | None:
        if value is not None:
            encode_value(_DURATION, value)
        return value

    @property
    def max_age_delta(self) -> timedelta | None:
        if self.max_age is None:
            return None
        delta = decode_value(_DURATION, encode_value(_DURATION, self.max_age))
        assert isinstance(delta, timedelta)
        return delta


class ReconcileOptions(_Options):
    """Detection of contradictions between Sources (design 10.6)."""

    enabled: bool = Field(default=True, description="Raise reconciliation candidates.")
    stages: tuple[Stage, ...] = Field(
        default=STAGE_ORDER,
        description="Stages run, always in the order identity, values, normalize, semantic; "
        "leave one out to disable it.",
    )
    triggers: TriggerOptions = Field(default_factory=TriggerOptions)
    hold_auto_accept: bool = Field(
        default=True,
        description="Extraction items that raise a contradiction stay pending whatever their "
        "confidence.",
    )
    sweep_interval_s: float = Field(
        default=0.0, ge=0, description="Scheduled sweep of every Domain; 0 disables it."
    )
    sweep_max_changes_per_job: int = Field(
        default=50000,
        ge=1,
        description="Branch history changes one sweep job reads; the rest goes to a continuation.",
    )
    max_claims_per_candidate: int = Field(
        default=20,
        ge=2,
        description="Claims kept per candidate, the newest; the others are only counted.",
    )
    duplicates: DuplicateOptions = Field(default_factory=DuplicateOptions)
    normalize: NormalizeOptions = Field(default_factory=NormalizeOptions)
    semantic: SemanticOptions = Field(default_factory=SemanticOptions)
    stale: StaleOptions = Field(default_factory=StaleOptions)
    merge_check: MergeCheck = Field(
        default="warn",
        description="Proposal check of open candidates for the Proposals Scribe merges or "
        "previews (Source syncs, extraction): off, warn or block. Set by the operator; a "
        "Domain's reconciliation_check setting can only make it stricter.",
    )

    @field_validator("stages")
    @classmethod
    def _ordered(cls, stages: tuple[Stage, ...]) -> tuple[Stage, ...]:
        if len(set(stages)) != len(stages):
            raise ValueError("stages must not repeat")
        return tuple(s for s in STAGE_ORDER if s in stages)

    def runs(self, stage: Stage) -> bool:
        """Whether ``stage`` runs (the semantic stage also needs ``semantic.enabled``)."""
        if not self.enabled or stage not in self.stages:
            return False
        return stage is not Stage.SEMANTIC or self.semantic.enabled


__all__ = [
    "STAGE_ORDER",
    "DuplicateOptions",
    "MergeCheck",
    "NormalizeOptions",
    "ReconcileOptions",
    "SemanticOptions",
    "Stage",
    "StaleOptions",
    "TriggerOptions",
]

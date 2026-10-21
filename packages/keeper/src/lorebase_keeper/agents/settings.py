"""The `agents` settings section: context packs, Record explanations and the change feed.

Registered with the platform like `read` and `search`; see `config/lorebase.example.yaml`.
Graph caps of context packs are further bounded by `read.traverse`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_keeper.agents.budget import DEFAULT_TRIM_ORDER, TrimStep
from lorebase_keeper.reading.settings import Bounds
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "agents"


class ContextSettings(SectionModel):
    """`POST /context`: token budget, graph caps and trimming order of context packs."""

    default_budget_tokens: int = Field(default=4000, ge=1, description="Budget when omitted.")
    max_budget_tokens: int = Field(default=32000, ge=1, description="Largest budget accepted.")
    chars_per_token: float = Field(
        default=4.0, gt=0, description="Characters per estimated token (tokens = chars / this)."
    )
    default_depth: int = Field(default=1, ge=0, description="Neighborhood depth when omitted.")
    max_depth: int = Field(default=2, ge=0, description="Deepest neighborhood accepted.")
    max_seeds: int = Field(default=20, ge=1, description="Seed Records of one pack.")
    max_neighbors: int = Field(default=200, ge=0, description="Records around the seeds.")
    max_links: int = Field(default=500, ge=0, description="Links of one pack.")
    recent_changes_max: int = Field(
        default=50, ge=1, description="Change entries read before trimming, newest first."
    )
    trim_order: tuple[TrimStep, ...] = Field(
        default=DEFAULT_TRIM_ORDER,
        description="Order in which sections are trimmed to fit the budget; steps left out are "
        "never applied. Seed Records are never trimmed.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.default_budget_tokens > self.max_budget_tokens:
            raise ValueError("default_budget_tokens cannot exceed max_budget_tokens")
        if self.default_depth > self.max_depth:
            raise ValueError("default_depth cannot exceed max_depth")
        if len(set(self.trim_order)) != len(self.trim_order):
            raise ValueError("trim_order lists a step twice")
        return self


class ExplainSettings(SectionModel):
    """`GET .../records/{key}/explain`."""

    max_links: int = Field(default=100, ge=0, description="Links listed with their origin.")
    max_proposals: int = Field(default=20, ge=0, description="Proposals listed.")


class FeedSettings(SectionModel):
    """Change feed of a branch: Server-Sent Events and the polling fallback."""

    poll_interval_s: float = Field(
        default=1.0, gt=0, description="How often an open stream looks for new events."
    )
    heartbeat_s: float = Field(
        default=15.0, gt=0, description="A comment line is sent after this much silence."
    )
    max_connection_s: float = Field(
        default=300.0,
        gt=0,
        description="A stream is closed after this long; clients reconnect with Last-Event-ID.",
    )
    reauthorize_interval_s: float = Field(
        default=60.0,
        gt=0,
        description="An open stream authenticates the caller and checks its grants again this "
        "often, through the same caches as a request, and closes once that fails: a revoked "
        "grant ends it within this interval plus `keeper.auth.authz.grants_cache_ttl_s`.",
    )
    retry_ms: int = Field(default=3000, ge=0, description="Reconnection delay advised to clients.")
    batch_events: int = Field(default=100, ge=1, description="Events read per poll.")
    changed_keys_max: int = Field(
        default=100, ge=0, description="Changed Record keys listed per event; more are cut."
    )
    changes: Bounds = Field(
        default_factory=lambda: Bounds(default_limit=50, max_limit=500),
        description="Revisions per page of `GET .../changes`.",
    )
    max_scan: int = Field(
        default=10_000,
        ge=1,
        description="First-parent Revisions walked back from the head to find `since`; an "
        "older `since` is refused (409: read the current state, then follow from the head).",
    )


class AgentSettings(SectionModel):
    """Agent-facing reads."""

    context: ContextSettings = Field(default_factory=ContextSettings)
    explain: ExplainSettings = Field(default_factory=ExplainSettings)
    feed: FeedSettings = Field(default_factory=FeedSettings)


register_section(SECTION, AgentSettings)


def agent_settings(settings: Settings) -> AgentSettings:
    """The validated `agents` section."""
    return settings.section(SECTION, AgentSettings)

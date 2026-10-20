"""The `agents` settings section: agent-facing reads such as context packs.

Registered with the platform like `read` and `search`; see `config/lorebase.example.yaml`.
Graph caps of context packs are further bounded by `read.traverse`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_keeper.agents.budget import DEFAULT_TRIM_ORDER, TrimStep
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


class AgentSettings(SectionModel):
    """Agent-facing reads."""

    context: ContextSettings = Field(default_factory=ContextSettings)


register_section(SECTION, AgentSettings)


def agent_settings(settings: Settings) -> AgentSettings:
    """The validated `agents` section."""
    return settings.section(SECTION, AgentSettings)

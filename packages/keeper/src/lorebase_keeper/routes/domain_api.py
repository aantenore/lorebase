"""Routers of the Domain-scoped endpoints (Domains, schema suggestions, schema, Operations,
reads, Derivation and Source previews, agent reads, webhooks, Sources, vault erasure, change feed,
health reports, Proposals, the reconciliation queue, Revisions, branches), in registration
order.

Routes taking a hierarchical branch name (`{branch:path}`) match any path suffix, so the
routers holding them come after routers with more specific branch sub-resources, and the
application includes these routers after its other Domain routers.
"""

from __future__ import annotations

from fastapi import APIRouter

from lorebase_keeper.routes import (
    agents,
    branches,
    derivations,
    documents,
    domains,
    events,
    kind_health,
    operations,
    proposal_items,
    proposals,
    read,
    reconciliation,
    revisions,
    schema,
    schema_inference,
    schema_suggestions,
    source_preview,
    sources,
    vault,
    webhooks,
)


def domain_routers() -> list[APIRouter]:
    """Routers to include under the API prefix, most specific paths first."""
    return [
        domains.router,
        schema_suggestions.router,  # `/schema/suggestions` before `/schema/{section}/...`
        schema.router,
        schema_inference.router,
        operations.router,
        read.router,
        derivations.router,
        kind_health.router,
        agents.router,
        webhooks.router,
        source_preview.router,
        sources.router,
        vault.router,
        documents.router,
        events.router,  # `/branches/{branch:path}/events` and `/changes` before the catch-all
        proposal_items.router,
        proposals.router,
        reconciliation.router,
        revisions.router,  # `/branches/{branch:path}/log` before the branch catch-all
        branches.router,
    ]

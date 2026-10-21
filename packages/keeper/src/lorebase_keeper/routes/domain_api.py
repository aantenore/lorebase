"""Routers of the Domain-scoped endpoints (Domains, schema, Operations, reads, agent reads,
webhooks, Sources, change feed, health reports, Proposals, Revisions, branches), in
registration order.

Routes taking a hierarchical branch name (`{branch:path}`) match any path suffix, so the
routers holding them come after routers with more specific branch sub-resources, and the
application includes these routers after its other Domain routers.
"""

from __future__ import annotations

from fastapi import APIRouter

from lorebase_keeper.routes import (
    agents,
    branches,
    documents,
    domains,
    events,
    kind_health,
    operations,
    proposal_items,
    proposals,
    read,
    revisions,
    schema,
    schema_inference,
    sources,
    webhooks,
)


def domain_routers() -> list[APIRouter]:
    """Routers to include under the API prefix, most specific paths first."""
    return [
        domains.router,
        schema.router,
        schema_inference.router,
        operations.router,
        read.router,
        kind_health.router,
        agents.router,
        webhooks.router,
        sources.router,
        documents.router,
        events.router,  # `/branches/{branch:path}/events` and `/changes` before the catch-all
        proposal_items.router,
        proposals.router,
        revisions.router,  # `/branches/{branch:path}/log` before the branch catch-all
        branches.router,
    ]

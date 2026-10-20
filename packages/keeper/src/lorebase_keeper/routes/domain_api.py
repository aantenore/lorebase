"""Routers of the Domain-scoped endpoints (Domains, schema, Operations, reads, Revisions,
branches), in registration order.

Routes taking a hierarchical branch name (`{branch:path}`) match any path suffix, so the
routers holding them come after routers with more specific branch sub-resources, and the
application includes these routers after its other Domain routers.
"""

from __future__ import annotations

from fastapi import APIRouter

from lorebase_keeper.routes import branches, domains, operations, read, revisions, schema


def domain_routers() -> list[APIRouter]:
    """Routers to include under the API prefix, most specific paths first."""
    return [
        domains.router,
        schema.router,
        operations.router,
        read.router,
        revisions.router,  # `/branches/{branch:path}/log` before the branch catch-all
        branches.router,
    ]

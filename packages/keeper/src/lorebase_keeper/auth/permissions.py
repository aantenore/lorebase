"""Permissions and the built-in Domain roles (design 10.2).

A role is a named set of permissions. Roles are granted per Domain (see `rbac`); installations
may add roles built from these permissions in `keeper.auth.authz.custom_roles`. The built-in
roles cannot be redefined.

Role semantics where the design table leaves room:

- every role except `admin` is additive on top of read access, so a principal holding only
  `editor` can still read what it edits;
- `maintainer` includes `editor` and `reviewer`, not `modeler`;
- `admin` holds every permission of the Domain (it is the Domain owner role).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType


class Permission(StrEnum):
    """What a route may require. Names are stable: they appear in 403 responses."""

    SCHEMA_READ = "schema.read"
    DATA_READ = "data.read"
    VIEWS_WRITE = "views.write"
    BRANCH_CREATE = "branch.create"
    OPERATION_EXECUTE = "operation.execute"
    OPERATION_EXECUTE_CUSTOM = "operation.execute_custom"
    SCHEMA_WRITE = "schema.write"
    SOURCE_MAP = "source.map"
    PROPOSAL_REVIEW = "proposal.review"
    CONFLICT_RESOLVE = "conflict.resolve"
    PROPOSAL_MERGE = "proposal.merge"
    SOURCE_MANAGE = "source.manage"
    JOB_MANAGE = "job.manage"
    GRANT_MANAGE = "grant.manage"
    APIKEY_MANAGE = "apikey.manage"
    DOMAIN_SETTINGS = "domain.settings"
    AUDIT_READ = "audit.read"
    PRINCIPAL_READ = "principal.read"


INSTALLATION_PERMISSIONS = frozenset({Permission.PRINCIPAL_READ})
"""Permissions that are not about one Domain: installation administrators hold them, and so do
principals who hold them through a role in at least one Domain."""

_VIEWER = frozenset({Permission.SCHEMA_READ, Permission.DATA_READ})
_EDITOR = _VIEWER | {Permission.BRANCH_CREATE, Permission.OPERATION_EXECUTE}
_REVIEWER = _VIEWER | {Permission.PROPOSAL_REVIEW, Permission.CONFLICT_RESOLVE}

BUILTIN_ROLES: Mapping[str, frozenset[Permission]] = MappingProxyType(
    {
        "viewer": _VIEWER,
        "explorer": _VIEWER | {Permission.VIEWS_WRITE},
        "editor": _EDITOR,
        "operator": _VIEWER | {Permission.OPERATION_EXECUTE_CUSTOM},
        "modeler": _VIEWER | {Permission.SCHEMA_WRITE, Permission.SOURCE_MAP},
        "reviewer": _REVIEWER,
        "maintainer": _EDITOR
        | _REVIEWER
        | {Permission.PROPOSAL_MERGE, Permission.SOURCE_MANAGE, Permission.JOB_MANAGE},
        "admin": frozenset(Permission),
        "agent-reader": _VIEWER,
    }
)


def permission_names() -> frozenset[str]:
    """Every permission name."""
    return frozenset(p.value for p in Permission)


def role_catalog(
    custom: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, frozenset[Permission]]:
    """Built-in roles plus `custom` ones. Unknown permissions raise `ValueError`."""
    catalog = dict(BUILTIN_ROLES)
    for name, permissions in (custom or {}).items():
        if name in BUILTIN_ROLES:
            raise ValueError(f"role {name!r} is built in and cannot be redefined")
        catalog[name] = frozenset(Permission(p) for p in permissions)
    return catalog

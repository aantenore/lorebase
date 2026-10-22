"""Permissions and the built-in Domain roles (design 10.2).

A role is a named set of permissions. Roles are granted per Domain (see `rbac`); installations
may add roles built from these permissions in `keeper.auth.authz.custom_roles`. The built-in
roles cannot be redefined.

Role semantics where the design table leaves room:

- every role except `admin` is additive on top of read access, so a principal holding only
  `editor` can still read what it edits;
- `maintainer` includes `editor` and `reviewer`, not `modeler`;
- `editor` and `modeler` open Proposals (`proposal.create`) from the branches they change;
  `editor` (and so `maintainer`) also triggers Source synchronizations (`source.sync`), whose
  outcome goes through the Source's merge policy;
  `editor` uploads documents for extraction (`document.write`) and reads them
  (`document.read`); `reviewer` reads documents too, since extraction items quote them;
  documents are raw, unmasked input, so `viewer`, `explorer` and `agent-reader` do not read
  them;
  `reviewer` reviews them and resolves Conflicts; merging needs `proposal.merge`
  (`maintainer`), into protected and unprotected branches alike;
- `admin` holds every permission of the Domain (it is the Domain owner role); only it may
  delete the Domain (`domain.delete`), read values of sensitive Attributes (`pii.read`,
  `confidential.read`, `restricted.read`; other callers see them masked) and erase vault
  values (`pii.erase`); custom roles grant the clearances to others. `admin` also reads the
  agent traces of other principals in its Domain (`trace.read`) and the query plans of traced
  requests (`trace.explain`); every principal reads its own traces. `domain.create` is
  installation-level: installation administrators hold it, and so do admins of any Domain;
- `vault.manage` (vault key rotation and its status) is held by installation administrators
  only: no role, built in or custom, contains it.
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
    PROPOSAL_CREATE = "proposal.create"
    PROPOSAL_REVIEW = "proposal.review"
    CONFLICT_RESOLVE = "conflict.resolve"
    PROPOSAL_MERGE = "proposal.merge"
    SOURCE_MANAGE = "source.manage"
    SOURCE_SYNC = "source.sync"
    DOCUMENT_READ = "document.read"
    DOCUMENT_WRITE = "document.write"
    JOB_MANAGE = "job.manage"
    GRANT_MANAGE = "grant.manage"
    APIKEY_MANAGE = "apikey.manage"
    DOMAIN_SETTINGS = "domain.settings"
    DOMAIN_DELETE = "domain.delete"
    DOMAIN_CREATE = "domain.create"
    AUDIT_READ = "audit.read"
    PRINCIPAL_READ = "principal.read"
    PII_READ = "pii.read"
    CONFIDENTIAL_READ = "confidential.read"
    RESTRICTED_READ = "restricted.read"
    PII_ERASE = "pii.erase"
    TRACE_READ = "trace.read"
    TRACE_EXPLAIN = "trace.explain"
    VAULT_MANAGE = "vault.manage"


INSTALLATION_ADMIN_PERMISSIONS = frozenset({Permission.VAULT_MANAGE})
"""Installation-level permissions only installation administrators hold: no role contains
them."""

INSTALLATION_PERMISSIONS = (
    frozenset({Permission.PRINCIPAL_READ, Permission.DOMAIN_CREATE})
    | INSTALLATION_ADMIN_PERMISSIONS
)
"""Permissions that are not about one Domain: installation administrators hold them, and so do
principals who hold them through a role in at least one Domain (except
`INSTALLATION_ADMIN_PERMISSIONS`)."""

_VIEWER = frozenset({Permission.SCHEMA_READ, Permission.DATA_READ})
_EDITOR = _VIEWER | {
    Permission.BRANCH_CREATE,
    Permission.OPERATION_EXECUTE,
    Permission.PROPOSAL_CREATE,
    Permission.SOURCE_SYNC,
    Permission.DOCUMENT_READ,
    Permission.DOCUMENT_WRITE,
}
_REVIEWER = _VIEWER | {
    Permission.PROPOSAL_REVIEW,
    Permission.CONFLICT_RESOLVE,
    Permission.DOCUMENT_READ,
}

BUILTIN_ROLES: Mapping[str, frozenset[Permission]] = MappingProxyType(
    {
        "viewer": _VIEWER,
        "explorer": _VIEWER | {Permission.VIEWS_WRITE},
        "editor": _EDITOR,
        "operator": _VIEWER | {Permission.OPERATION_EXECUTE_CUSTOM},
        "modeler": _VIEWER
        | {Permission.SCHEMA_WRITE, Permission.SOURCE_MAP, Permission.PROPOSAL_CREATE},
        "reviewer": _REVIEWER,
        "maintainer": _EDITOR
        | _REVIEWER
        | {Permission.PROPOSAL_MERGE, Permission.SOURCE_MANAGE, Permission.JOB_MANAGE},
        "admin": frozenset(Permission) - INSTALLATION_ADMIN_PERMISSIONS,
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
        granted = frozenset(Permission(p) for p in permissions)
        reserved = granted & INSTALLATION_ADMIN_PERMISSIONS
        if reserved:
            names = ", ".join(sorted(p.value for p in reserved))
            raise ValueError(f"role {name!r}: {names} is held by installation administrators only")
        catalog[name] = granted
    return catalog

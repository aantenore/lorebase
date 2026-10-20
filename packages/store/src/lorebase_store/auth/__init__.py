"""Repositories of principals, groups, API keys, grants and the audit log."""

from lorebase_store.auth.api_keys import ApiKeyRecord, ApiKeyRepository, PrefixTakenError
from lorebase_store.auth.audit import (
    AuditRecord,
    AuditRepository,
    ChainVerification,
    NewAuditEntry,
    audit_lock_key,
    entry_digest,
)
from lorebase_store.auth.grants import GrantRecord, GrantRepository
from lorebase_store.auth.principals import (
    GroupRepository,
    PrincipalRecord,
    PrincipalRepository,
)
from lorebase_store.auth.scope import new_id, rls_scope
from lorebase_store.auth.unit import AuthUnit, SqlAuthStore

__all__ = [
    "ApiKeyRecord",
    "ApiKeyRepository",
    "AuditRecord",
    "AuditRepository",
    "AuthUnit",
    "ChainVerification",
    "GrantRecord",
    "GrantRepository",
    "GroupRepository",
    "NewAuditEntry",
    "PrefixTakenError",
    "PrincipalRecord",
    "PrincipalRepository",
    "SqlAuthStore",
    "audit_lock_key",
    "entry_digest",
    "new_id",
    "rls_scope",
]

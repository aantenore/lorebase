"""Fixtures for the auth tests: an in-memory store, API key settings and a fake OIDC provider."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.exc import IntegrityError

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import (
    ApiKeyRecord,
    AuditRecord,
    ChainVerification,
    GrantRecord,
    NewAuditEntry,
    PrefixTakenError,
    PrincipalRecord,
    SessionRecord,
    UsageRecord,
    entry_digest,
)
from lorebase_store.models.enums import GrantScope, PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
ISSUER = "https://idp.example.test"
AUDIENCE = "lorebase-api"
JWKS_URI = f"{ISSUER}/keys"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


class MemPrincipals:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def get(self, principal_id: str) -> PrincipalRecord | None:
        return self.s.principal_rows.get(principal_id)

    async def get_by_subject(self, subject: str) -> PrincipalRecord | None:
        return next(
            (p for p in self.s.principal_rows.values() if p.external_subject == subject), None
        )

    async def add(
        self,
        type_: PrincipalType,
        display_name: str,
        *,
        external_subject: str | None = None,
        principal_id: str | None = None,
    ) -> PrincipalRecord:
        prefix = {PrincipalType.USER: "usr", PrincipalType.SERVICE: "svc"}[type_]
        return self.s.add_principal(
            principal_id or f"{prefix}_{uuid.uuid4().hex[:8]}",
            type_,
            external_subject,
            display_name,
        )

    async def get_or_create_user(self, subject: str, name: str) -> tuple[PrincipalRecord, bool]:
        existing = await self.get_by_subject(subject)
        if existing:
            return existing, False
        return await self.add(PrincipalType.USER, name, external_subject=subject), True

    async def list(
        self, *, type_: PrincipalType | None = None, limit: int, after: Any = None
    ) -> list[PrincipalRecord]:
        rows = sorted(self.s.principal_rows.values(), key=lambda p: (p.created_at, p.id))
        rows = [p for p in rows if type_ is None or p.type is type_]
        if after is not None:
            rows = [p for p in rows if (p.created_at, p.id) > after]
        return rows[:limit]


class MemGroups:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def group_ids_for(self, principal_id: str, external: Collection[str] = ()) -> list[str]:
        ids = {g for g, p in self.s.members if p == principal_id}
        ids |= {g for g, ext in self.s.group_rows.items() if ext in set(external)}
        return sorted(ids)

    async def subject_exists(self, subject_id: str) -> bool:
        return subject_id in self.s.principal_rows or subject_id in self.s.group_rows


class MemKeys:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def add(
        self,
        *,
        principal_id: str,
        prefix: str,
        key_hash: bytes,
        scopes: Sequence[str] = (),
        expires_at: datetime | None = None,
        allowed_networks: Sequence[str] = (),
    ) -> ApiKeyRecord:
        if any(k.prefix == prefix for k in self.s.key_rows.values()):
            raise PrefixTakenError(prefix)
        return self.s.put_key(
            principal_id, prefix, key_hash, scopes, expires_at, None, allowed_networks
        )

    async def get(self, key_id: str) -> ApiKeyRecord | None:
        return self.s.key_rows.get(key_id)

    async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        return next((k for k in self.s.key_rows.values() if k.prefix == prefix), None)

    async def touch(self, key_id: str, at: datetime, *, min_interval: timedelta) -> bool:
        if self.s.touch_failure is not None:
            raise self.s.touch_failure
        key = self.s.key_rows[key_id]
        if key.last_used_at is not None and key.last_used_at > at - min_interval:
            return False
        self.s.key_rows[key_id] = replace(key, last_used_at=at)
        self.s.touches += 1
        return True

    async def revoke(self, key_id: str, at: datetime) -> bool:
        key = self.s.key_rows.get(key_id)
        if key is None or key.revoked_at is not None:
            return False
        self.s.key_rows[key_id] = replace(key, revoked_at=at)
        return True

    async def list_for_scope(
        self, scope: str, *, limit: int, after: Any = None
    ) -> list[ApiKeyRecord]:
        rows = sorted(self.s.key_rows.values(), key=lambda k: (k.created_at, k.id))
        rows = [k for k in rows if scope in k.scopes]
        if after is not None:
            rows = [k for k in rows if (k.created_at, k.id) > after]
        return rows[:limit]

    async def list_for_principals(self, principal_ids: Collection[str]) -> list[ApiKeyRecord]:
        rows = sorted(self.s.key_rows.values(), key=lambda k: (k.created_at, k.id))
        return [k for k in rows if k.principal_id in principal_ids]


class MemGrants:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def add(
        self,
        *,
        domain_id: str,
        subject: str,
        role: str,
        created_by: str,
        scope_type: GrantScope = GrantScope.DOMAIN,
        scope_ref: str | None = None,
    ) -> GrantRecord:
        if domain_id not in self.s.domains:
            raise IntegrityError("insert", {}, Exception("foreign key"))
        return self.s.add_grant(domain_id, subject, role, scope_type, scope_ref, created_by)

    async def get(self, domain_id: str, grant_id: str) -> GrantRecord | None:
        return next(
            (g for g in self.s.grant_rows if g.domain_id == domain_id and g.id == grant_id), None
        )

    async def delete(self, domain_id: str, grant_id: str) -> bool:
        before = len(self.s.grant_rows)
        self.s.grant_rows[:] = [
            g for g in self.s.grant_rows if not (g.domain_id == domain_id and g.id == grant_id)
        ]
        return len(self.s.grant_rows) < before

    async def for_subjects(self, domain_id: str, subjects: Collection[str]) -> list[GrantRecord]:
        self.s.grant_lookups += 1
        return [
            g for g in self.s.grant_rows if g.domain_id == domain_id and g.subject in set(subjects)
        ]

    async def domain_exists(self, domain_id: str) -> bool:
        return domain_id in self.s.domains

    async def for_subjects_anywhere(self, subjects: Collection[str]) -> list[GrantRecord]:
        self.s.grant_lookups += 1
        return [g for g in self.s.grant_rows if g.subject in set(subjects)]

    async def list_domain(
        self, domain_id: str, *, limit: int, after: Any = None
    ) -> list[GrantRecord]:
        rows = sorted(
            (g for g in self.s.grant_rows if g.domain_id == domain_id),
            key=lambda g: (g.created_at, g.id),
        )
        if after is not None:
            rows = [g for g in rows if (g.created_at, g.id) > after]
        return rows[:limit]


class MemAudit:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def append(self, entry: NewAuditEntry) -> AuditRecord:
        if self.s.audit_delay:
            await asyncio.sleep(self.s.audit_delay)
        if self.s.audit_failure is not None:
            raise self.s.audit_failure
        chain = [r for r in self.s.audit_rows if r.domain_id == entry.domain_id]
        prev_hash = chain[-1].entry_hash if chain else None
        content: dict[str, Any] = {
            "domain_id": entry.domain_id,
            "at": entry.at or self.s.tick(),
            "actor": entry.actor,
            "action": entry.action,
            "target": entry.target,
            "details": dict(entry.details),
        }
        record = AuditRecord(
            seq=len(self.s.audit_rows) + 1,
            prev_hash=prev_hash,
            entry_hash=entry_digest(prev_hash, **content),
            **content,
        )
        self.s.audit_rows.append(record)
        return record

    async def verify(self, *, after_seq: int = 0, domain_id: str | None) -> ChainVerification:
        """The walk of `AuditRepository.verify` over one chain of the in-memory rows."""
        rows = [r for r in self.s.audit_rows if r.domain_id == domain_id and r.seq > after_seq]
        head = rows[0].prev_hash if rows and after_seq > 0 else None
        last: int | None = None
        for checked, row in enumerate(rows):
            digest = entry_digest(
                row.prev_hash,
                domain_id=row.domain_id,
                at=row.at,
                actor=row.actor,
                action=row.action,
                target=row.target,
                details=row.details,
            )
            reason = (
                "chain link broken"
                if row.prev_hash != head
                else "entry content changed"
                if digest != row.entry_hash
                else None
            )
            if reason is not None:
                return ChainVerification(
                    checked, row.seq, reason, chains=1, last_seq=last, bad_domain_id=domain_id
                )
            head, last = row.entry_hash, row.seq
        return ChainVerification(len(rows), chains=1 if rows else 0, last_seq=last)

    async def list(
        self, *, domain_id: str | None, limit: int, before_seq: int | None = None
    ) -> list[AuditRecord]:
        rows = [r for r in reversed(self.s.audit_rows) if r.domain_id == domain_id]
        if before_seq is not None:
            rows = [r for r in rows if r.seq < before_seq]
        return rows[:limit]


class MemUsage:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def get(self, api_key_id: str, day: date) -> UsageRecord:
        self.s.usage_reads += 1
        if self.s.usage_failure is not None:
            raise self.s.usage_failure
        return self.s.usage_rows.get((api_key_id, day), UsageRecord(api_key_id, day, 0, 0))

    async def add(
        self, api_key_id: str, day: date, *, requests: int, context_tokens: int, at: datetime
    ) -> UsageRecord:
        self.s.usage_writes += 1
        if self.s.usage_failure is not None:
            raise self.s.usage_failure
        before = await self.get(api_key_id, day)
        row = UsageRecord(
            api_key_id, day, before.requests + requests, before.context_tokens + context_tokens
        )
        self.s.usage_rows[(api_key_id, day)] = row
        return row


class MemSessions:
    def __init__(self, store: MemoryStore) -> None:
        self.s = store

    async def add(
        self,
        *,
        id_hash: bytes,
        principal_id: str,
        subject: str,
        external_groups: Sequence[str],
        tokens: bytes | None,
        created_at: datetime,
        expires_at: datetime,
        refresh_at: datetime | None,
    ) -> SessionRecord:
        record = SessionRecord(
            id_hash=id_hash,
            principal_id=principal_id,
            subject=subject,
            external_groups=tuple(external_groups),
            tokens=tokens,
            created_at=created_at,
            last_seen_at=created_at,
            expires_at=expires_at,
            refresh_at=refresh_at,
        )
        self.s.session_rows[id_hash] = record
        return record

    async def get(self, id_hash: bytes) -> SessionRecord | None:
        return self.s.session_rows.get(id_hash)

    async def touch(self, id_hash: bytes, at: datetime, *, min_interval: timedelta) -> bool:
        row = self.s.session_rows.get(id_hash)
        if row is None or row.last_seen_at > at - min_interval:
            return False
        self.s.session_rows[id_hash] = replace(row, last_seen_at=at)
        return True

    async def claim_refresh(self, id_hash: bytes, now: datetime, until: datetime) -> bool:
        row = self.s.session_rows.get(id_hash)
        if row is None or row.refresh_at is None or row.refresh_at > now:
            return False
        self.s.session_rows[id_hash] = replace(row, refresh_at=until)
        return True

    async def update_tokens(
        self,
        id_hash: bytes,
        *,
        tokens: bytes | None,
        external_groups: Sequence[str],
        refresh_at: datetime | None,
    ) -> bool:
        row = self.s.session_rows.get(id_hash)
        if row is None:
            return False
        self.s.session_rows[id_hash] = replace(
            row, tokens=tokens, external_groups=tuple(external_groups), refresh_at=refresh_at
        )
        return True

    async def delete(self, id_hash: bytes) -> bool:
        return self.s.session_rows.pop(id_hash, None) is not None

    async def purge_expired(self, now: datetime, idle_before: datetime, *, limit: int) -> int:
        stale = [
            key
            for key, row in self.s.session_rows.items()
            if row.expires_at <= now or row.last_seen_at <= idle_before
        ][:limit]
        for key in stale:
            del self.s.session_rows[key]
        return len(stale)


class MemoryUnit:
    """Auth repositories over a `MemoryStore`; there are no transactions."""

    def __init__(self, store: MemoryStore) -> None:
        self.principals = MemPrincipals(store)
        self.groups = MemGroups(store)
        self.api_keys = MemKeys(store)
        self.grants = MemGrants(store)
        self.audit = MemAudit(store)
        self.usage = MemUsage(store)
        self.sessions = MemSessions(store)


class MemoryStore:
    """In-memory implementation of the `AuthStore` port, with seeding and inspection helpers."""

    def __init__(self) -> None:
        self.principal_rows: dict[str, PrincipalRecord] = {}
        self.group_rows: dict[str, str | None] = {}
        self.members: set[tuple[str, str]] = set()
        self.key_rows: dict[str, ApiKeyRecord] = {}
        self.grant_rows: list[GrantRecord] = []
        self.audit_rows: list[AuditRecord] = []
        self.audit_failure: Exception | None = None
        self.audit_delay = 0.0
        self.touch_failure: Exception | None = None
        self.usage_rows: dict[tuple[str, date], UsageRecord] = {}
        self.session_rows: dict[bytes, SessionRecord] = {}
        self.usage_failure: Exception | None = None
        self.usage_reads = 0
        self.usage_writes = 0
        self.domains: set[str] = {"acme", "globex"}
        self.touches = 0
        self.grant_lookups = 0
        self._clock = NOW

    def tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    @asynccontextmanager
    async def unit(self) -> AsyncIterator[MemoryUnit]:
        yield MemoryUnit(self)

    def add_principal(
        self,
        principal_id: str,
        type_: PrincipalType = PrincipalType.USER,
        subject: str | None = None,
        name: str | None = None,
    ) -> PrincipalRecord:
        record = PrincipalRecord(principal_id, type_, name or principal_id, subject, self.tick())
        self.principal_rows[principal_id] = record
        return record

    def add_grant(
        self,
        domain_id: str,
        subject: str,
        role: str,
        scope_type: GrantScope = GrantScope.DOMAIN,
        scope_ref: str | None = None,
        created_by: str = "usr_seed",
    ) -> GrantRecord:
        grant = GrantRecord(
            id=f"grt_{len(self.grant_rows) + 1}",
            domain_id=domain_id,
            subject=subject,
            role=role,
            scope_type=scope_type,
            scope_ref=scope_ref,
            created_by=created_by,
            created_at=self.tick(),
        )
        self.grant_rows.append(grant)
        return grant

    def put_key(
        self,
        principal_id: str,
        prefix: str,
        key_hash: bytes,
        scopes: Sequence[str],
        expires_at: datetime | None,
        revoked_at: datetime | None,
        allowed_networks: Sequence[str] = (),
    ) -> ApiKeyRecord:
        record = ApiKeyRecord(
            id=f"key_{len(self.key_rows) + 1}",
            principal_id=principal_id,
            prefix=prefix,
            hash=key_hash,
            scopes=tuple(scopes),
            expires_at=expires_at,
            last_used_at=None,
            revoked_at=revoked_at,
            created_at=self.tick(),
            allowed_networks=tuple(allowed_networks),
        )
        self.key_rows[record.id] = record
        return record

    def add_key(
        self,
        codec: ApiKeyCodec,
        principal_id: str,
        *,
        scopes: Sequence[str] = (),
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
        allowed_networks: Sequence[str] = (),
    ) -> tuple[str, ApiKeyRecord]:
        """Issue a key through `codec`; returns the token and the stored row."""
        generated = codec.generate()
        record = self.put_key(
            principal_id,
            generated.prefix,
            generated.key_hash,
            scopes,
            expires_at,
            revoked_at,
            allowed_networks,
        )
        return generated.token, record


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def codec() -> ApiKeyCodec:
    return ApiKeyCodec(ApiKeySettings(pepper=PEPPER))


class Provider:
    """A signing key, its published JWKS and a mock HTTP transport serving both."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.requests: list[str] = []
        self.fail = False
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def jwk(self) -> dict[str, Any]:
        document = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {**document, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.fail:
            return httpx.Response(500)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        return httpx.Response(404)

    def rotate(self, kid: str) -> None:
        """Publish a new signing key under a new id (the old one disappears)."""
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = kid

    def token(self, *, key: Any = None, kid: str | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "alice-subject",
            "name": "Alice",
            "iat": now,
            "exp": now + 600,
            **overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims, key or self.private_key, algorithm="RS256", headers={"kid": kid or self.kid}
        )

    def count(self, path: str) -> int:
        return self.requests.count(path)


@pytest.fixture(scope="module")
def _module_provider() -> Provider:
    """RSA key generation is slow: one provider per test module."""
    return Provider()


@pytest.fixture
def provider(_module_provider: Provider) -> Provider:
    """The fake provider with its request log and failure switch reset."""
    _module_provider.requests.clear()
    _module_provider.fail = False
    _module_provider.kid = "key-1"
    return _module_provider


class Clock:
    """A settable wall clock for expiry tests."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


AppBuilder = Callable[..., FastAPI]


@pytest.fixture
def auth_app(
    make_app: Callable[..., FastAPI], store: MemoryStore, provider: Provider, clock: Clock
) -> AppBuilder:
    """Build an application on the in-memory store, the fake provider and the test clock."""

    def build(auth: dict[str, Any] | None = None, **kwargs: Any) -> FastAPI:
        kwargs.setdefault("auth_store", store)
        kwargs.setdefault("http_client", provider.http)
        kwargs.setdefault("clock", clock)
        return make_app({"auth": auth or {}}, **kwargs)

    return build

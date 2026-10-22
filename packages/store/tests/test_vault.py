"""The personal data vault against Postgres (design 13.5): tokens in objects, values in the
vault, erasure across history, references bound to their Record, row-level security, and no
sensitive word in the search vectors, the lexicon or search corrections."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record, Sensitivity, vault_reference, vault_token
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode, RecordUpsert
from lorebase_store.commit import (
    CommitResult,
    SchemaUpsert,
    SensitivityLoweredError,
    ValidationFailedError,
    commit,
    create_domain,
)
from lorebase_store.materialize import verify
from lorebase_store.models import MatLexicon, MatRecord, StoredObject
from lorebase_store.reads import TypoTolerance, search_corrections
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings
from lorebase_store.vault import (
    VaultKeys,
    VaultSettings,
    VaultState,
    erase_matching,
    erase_record,
    open_values,
)

from .support import (
    AUTHOR,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    SUPPLIER,
    SUPPLIER_CODE,
    Clock,
    attribute,
    domain,
    ident,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
EMAIL = ident("att", 190)
NICKNAME = ident("att", 191)
PII = (Sensitivity.PII,)
VAULT = VaultSettings.model_validate(
    {"token_key": "t" * 32, "encryption_key": "e" * 32, "key_id": "k1"}
)


def kind() -> Any:
    base = supplier_kind()
    return base.model_copy(
        update={
            "attributes": {
                **base.attributes,
                EMAIL: attribute(
                    EMAIL, "email", TypeSpec.of("string"), sensitivity=PII, vault=True
                ),
                NICKNAME: attribute(
                    NICKNAME, "nickname", TypeSpec.of("string"), sensitivity=PII, searchable=True
                ),
            }
        }
    )


def person(code: str, email: Any = None, nickname: str | None = None) -> RecordUpsert:
    values: dict[str, Any] = {SUPPLIER_CODE: code, LEGAL_NAME: f"Company {code}"}
    if email is not None:
        values[EMAIL] = email
    if nickname is not None:
        values[NICKNAME] = nickname
    return RecordUpsert(record=Record(kind=SUPPLIER, key=code, values=values))


@pytest.fixture
def runtime(store: StoreSettings) -> StoreRuntime:
    return StoreRuntime.from_settings(store, vault=VAULT)


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> AsyncIterator[Sessions]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    async with factory() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            author=AUTHOR,
            message="Schema",
            origin=ORIGIN,
            schema_changes=[SchemaUpsert(kind())],
            runtime=runtime,
            clock=clock,
        )
    yield factory
    async with factory() as session:
        report = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
    assert report.ok, report.divergences


async def write(sessions: Sessions, runtime: StoreRuntime, *changes: RecordUpsert) -> CommitResult:
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            "main",
            list(changes),
            author=AUTHOR,
            message="Write",
            origin=ORIGIN,
            runtime=runtime,
        )


async def stored_email(sessions: Sessions, code: str) -> Any:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        row = await session.scalar(
            select(MatRecord.values).where(
                MatRecord.domain_id == DOMAIN, MatRecord.branch == "main", MatRecord.key == code
            )
        )
        assert row is not None
        return row.get(EMAIL)


async def opened(sessions: Sessions, runtime: StoreRuntime, *tokens: str) -> dict[str, Any]:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        found = await open_values(session, DOMAIN, tokens, runtime.vault)
    return {t: (v.state, v.value) for t, v in found.items()}


async def test_values_live_in_the_vault_and_objects_hold_tokens(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(
        sessions, runtime, person("S-1", "ada@example.com"), person("S-2", "ada@example.com")
    )
    first = vault_token(await stored_email(sessions, "S-1"))
    second = vault_token(await stored_email(sessions, "S-2"))
    assert first
    assert second
    assert first != second
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        bodies = [str(b) for b in (await session.scalars(select(StoredObject.body))).all()]
    assert not any("ada@example.com" in body for body in bodies)
    assert await opened(sessions, runtime, first) == {first: (VaultState.VALUE, "ada@example.com")}

    # Writing the same value again changes nothing; a new value gets a new token.
    again = await write(sessions, runtime, person("S-1", "ada@example.com"))
    assert again.no_op
    await write(sessions, runtime, person("S-1", "ada@new.example"))
    newer = vault_token(await stored_email(sessions, "S-1"))
    assert newer not in (None, first)


async def test_erasure_reaches_every_revision_and_is_idempotent(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "old@example.com"))
    old = vault_token(await stored_email(sessions, "S-1"))
    await write(sessions, runtime, person("S-1", "new@example.com"))
    new = vault_token(await stored_email(sessions, "S-1"))
    assert old
    assert new
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        assert await erase_record(session, DOMAIN, SUPPLIER, "S-1") == 2
        assert await erase_record(session, DOMAIN, SUPPLIER, "S-1") == 0
    assert await opened(sessions, runtime, old, new) == {
        old: (VaultState.ERASED, None),
        new: (VaultState.ERASED, None),
    }
    # The erased value written again is stored under a new token; history stays erased.
    await write(sessions, runtime, person("S-1", "old@example.com"))
    again = vault_token(await stored_email(sessions, "S-1"))
    assert again not in (None, old, new)
    assert (await opened(sessions, runtime, again))[again][0] is VaultState.VALUE  # type: ignore[index]
    assert (await opened(sessions, runtime, old))[old][0] is VaultState.ERASED


async def test_erasure_by_value_reaches_every_record(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(
        sessions,
        runtime,
        person("S-1", "shared@example.com"),
        person("S-2", "shared@example.com"),
        person("S-3", "other@example.com"),
    )
    keys = runtime.vault
    assert keys is not None
    fingerprint = keys.fingerprint(DOMAIN, SUPPLIER, EMAIL, "shared@example.com")
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        assert await erase_matching(session, DOMAIN, SUPPLIER, EMAIL, fingerprint) == 2
    tokens = [vault_token(await stored_email(sessions, c)) for c in ("S-1", "S-2", "S-3")]
    states = await opened(sessions, runtime, *[t for t in tokens if t])
    assert [states[t][0] for t in tokens if t] == [
        VaultState.ERASED,
        VaultState.ERASED,
        VaultState.VALUE,
    ]


async def test_references_are_bound_to_their_record(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"), person("S-2"))
    reference = await stored_email(sessions, "S-1")
    # Rewriting a Record with the reference it holds is fine; copying it to another is not.
    assert (await write(sessions, runtime, person("S-1", reference))).no_op
    with pytest.raises(ValidationFailedError) as raised:
        await write(sessions, runtime, person("S-2", reference))
    assert [i.code for i in raised.value.issues] == [IssueCode.VALUE_VAULT_REFERENCE]
    with pytest.raises(ValidationFailedError) as forged:
        await write(sessions, runtime, person("S-2", vault_reference("vlt_" + "1" * 32)))
    assert [i.code for i in forged.value.issues] == [IssueCode.VALUE_VAULT_REFERENCE]


async def test_vault_values_need_the_keys(sessions: Sessions, store: StoreSettings) -> None:
    without = StoreRuntime.from_settings(store)
    with pytest.raises(ValidationFailedError) as raised:
        await write(sessions, without, person("S-1", "ada@example.com"))
    assert [i.code for i in raised.value.issues] == [IssueCode.VALUE_VAULT_UNAVAILABLE]
    keys = VaultKeys("t" * 32, "k2", "x" * 32, {})
    await write(sessions, StoreRuntime.from_settings(store, vault=VAULT), person("S-1", "a@b.c"))
    token = vault_token(await stored_email(sessions, "S-1"))
    assert token
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        found = await open_values(session, DOMAIN, [token], keys)
    assert found[token].state is VaultState.UNAVAILABLE  # sealed under a key not configured


async def test_sensitive_words_stay_out_of_search(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com", nickname="Zanzibarella"))
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        words = set((await session.scalars(select(MatLexicon.word))).all())
        vectors = [str(v) for v in (await session.scalars(select(MatRecord.search))).all()]
        typos = TypoTolerance(threshold=0.3, weight=0.5, candidates=3, min_length=3)
        fixes = await search_corrections(
            session, DOMAIN, "main", "Zanzibarela", typos, runtime=runtime
        )
    assert "compani" in words or "company" in words  # the legal name is indexed
    assert not any("zanzibar" in w for w in words)
    assert not any("zanzibar" in v.lower() or "example" in v.lower() for v in vectors)
    assert all("zanzibar" not in r for c in fixes for r in c.replacements)


async def test_row_level_security_scopes_vault_rows(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    role = f"lorebase_vault_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await connection.execute(text(f"GRANT SELECT ON vault TO {role}"))
    try:
        for scope, expected in (("", 0), ("other", 0), (DOMAIN, 1)):
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
                await connection.execute(
                    text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": scope}
                )
                seen = await connection.scalar(text("SELECT count(*) FROM vault"))
            assert seen == expected, scope
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))


async def test_vault_storage_is_fixed_when_an_attribute_is_created(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    current = kind()
    plain = current.attributes[EMAIL].model_copy(update={"vault": None})
    changed = current.model_copy(update={"attributes": {**current.attributes, EMAIL: plain}})
    async with sessions() as session:
        with pytest.raises(ValidationFailedError) as raised:
            await commit(
                session,
                DOMAIN,
                "main",
                [],
                author=AUTHOR,
                message="Vault off",
                origin=ORIGIN,
                schema_changes=[SchemaUpsert(changed)],
                runtime=runtime,
            )
    assert IssueCode.SCHEMA_VAULT_INVALID in {i.code for i in raised.value.issues}


async def test_removing_a_tag_needs_to_be_allowed(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    current = kind()
    plain = current.attributes[NICKNAME].model_copy(update={"sensitivity": ()})
    changed = current.model_copy(update={"attributes": {**current.attributes, NICKNAME: plain}})

    async def untag(allowed: frozenset[Sensitivity]) -> None:
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                "main",
                [],
                author=AUTHOR,
                message="Untag",
                origin=ORIGIN,
                schema_changes=[SchemaUpsert(changed)],
                runtime=runtime,
                allowed_lowering=allowed,
            )

    with pytest.raises(SensitivityLoweredError) as raised:
        await untag(frozenset())
    assert raised.value.tags == ["pii"]
    with pytest.raises(SensitivityLoweredError):  # cleared for another tag only
        await untag(frozenset({Sensitivity.CONFIDENTIAL}))
    await untag(frozenset(PII))  # the caller is cleared for pii

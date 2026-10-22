"""The personal data vault against Postgres (design 13.5): tokens in objects, values in the
vault, erasure across history, references bound to their Record, row-level security, no
sensitive word in the search vectors, the lexicon or search corrections, erasure of case
variants, key changes and later writes, the erasure lock, and key rotation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record, Sensitivity, vault_reference, vault_token
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode, RecordDelete, RecordUpsert
from lorebase_store.commit import (
    CommitResult,
    SchemaUpsert,
    SensitivityLoweredError,
    ValidationFailedError,
    commit,
    create_domain,
)
from lorebase_store.materialize import BranchMode, create_branch, verify
from lorebase_store.models import MatLexicon, MatRecord, StoredObject, VaultEntry
from lorebase_store.reads import TypoTolerance, search_corrections
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings
from lorebase_store.vault import (
    ErasedValues,
    VaultKeys,
    VaultSettings,
    VaultState,
    erase_matching,
    erase_record,
    open_values,
    rotate,
    usage,
)
from lorebase_store.vault.store import MAX_LINKED_KEYS, VaultLockTimeoutError

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
    {
        "token_key": "t" * 32,
        "encryption_key": "e" * 32,
        "key_id": "k1",
        # The commits of these tests stand for Source syncs: their origin is checked.
        "tombstone_origins": ["test"],
    }
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


async def write(
    sessions: Sessions,
    runtime: StoreRuntime,
    *changes: RecordUpsert | RecordDelete,
    branch: str = "main",
) -> CommitResult:
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            branch,
            list(changes),
            author=AUTHOR,
            message="Write",
            origin=ORIGIN,
            runtime=runtime,
        )


async def stored_email(sessions: Sessions, code: str, branch: str = "main") -> Any:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        row = await session.scalar(
            select(MatRecord.values).where(
                MatRecord.domain_id == DOMAIN, MatRecord.branch == branch, MatRecord.key == code
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
        assert (await erase_record(session, DOMAIN, SUPPLIER, "S-1")).erased == 2
        assert (await erase_record(session, DOMAIN, SUPPLIER, "S-1")).erased == 0
    assert await opened(sessions, runtime, old, new) == {
        old: (VaultState.ERASED, None),
        new: (VaultState.ERASED, None),
    }
    # Written again (a Source sync, say), the erased value stays erased: same token.
    await write(sessions, runtime, person("S-1", "new@example.com"))
    assert vault_token(await stored_email(sessions, "S-1")) == new
    # Allowed again, it is stored under a new token; history stays erased.
    allowing = with_vault(runtime, erased_values=ErasedValues.ALLOW)
    await write(sessions, allowing, person("S-1", "old@example.com"))
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
    assert await erase_value(sessions, runtime, "shared@example.com") == 2
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


def with_vault(runtime: StoreRuntime, **changes: Any) -> StoreRuntime:
    """A runtime sharing the settings of `runtime` with other vault settings."""
    settings = VAULT.model_copy(update=changes)
    return StoreRuntime.from_settings(
        runtime.settings,
        vault=VaultSettings.model_validate(
            settings.model_dump(mode="python")
            | {
                "token_key": _secret(settings.token_key),
                "encryption_key": _secret(settings.encryption_key),
                "digest_key": _secret(settings.digest_key),
                "previous_keys": {k: _secret(v) for k, v in settings.previous_keys.items()},
                "previous_token_keys": {
                    k: _secret(v) for k, v in settings.previous_token_keys.items()
                },
            }
        ),
    )


def _secret(value: Any) -> Any:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


async def erase_value(sessions: Sessions, runtime: StoreRuntime, value: Any) -> int:
    keys = runtime.vault
    assert keys is not None
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        return (await erase_matching(session, DOMAIN, SUPPLIER, EMAIL, value, keys)).erased


async def erase_key(sessions: Sessions, key: str) -> int:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        return (await erase_record(session, DOMAIN, SUPPLIER, key)).erased


async def state_of(sessions: Sessions, runtime: StoreRuntime, code: str) -> VaultState:
    token = vault_token(await stored_email(sessions, code))
    assert token
    return (await opened(sessions, runtime, token))[token][0]  # type: ignore[no-any-return]


async def test_erasure_by_value_reaches_case_variants(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(
        sessions,
        runtime,
        person("S-1", "Ada.Lovelace@Example.com"),
        person("S-2", "  ada.lovelace@example.COM "),
        person("S-3", "ada.lovelace@example.org"),
    )
    assert await erase_value(sessions, runtime, "ada.lovelace@example.com") == 2
    assert [await state_of(sessions, runtime, c) for c in ("S-1", "S-2", "S-3")] == [
        VaultState.ERASED,
        VaultState.ERASED,
        VaultState.VALUE,
    ]
    # A later write of a variant of the erased value, to any Record of the Kind, stays erased.
    await write(sessions, runtime, person("S-4", "ADA.LOVELACE@EXAMPLE.COM"))
    assert await state_of(sessions, runtime, "S-4") is VaultState.ERASED
    refusing = with_vault(runtime, erased_values=ErasedValues.REFUSE)
    with pytest.raises(ValidationFailedError) as raised:
        await write(sessions, refusing, person("S-5", "ada.lovelace@example.com"))
    assert [i.code for i in raised.value.issues] == [IssueCode.VALUE_VAULT_ERASED]
    # Writes of other origins (people, agents) are not checked: the outcome would tell the
    # writer that a guessed value was erased.
    unchecked = with_vault(runtime, erased_values=ErasedValues.REFUSE, tombstone_origins=())
    await write(sessions, unchecked, person("S-5", "ada.lovelace@example.com"))
    assert await state_of(sessions, runtime, "S-5") is VaultState.VALUE


async def test_erasure_follows_key_changes(sessions: Sessions, runtime: StoreRuntime) -> None:
    await write(sessions, runtime, person("S-1", "old@example.com"))
    await write(sessions, runtime, person("S-1", "kept@example.com"))
    held = await stored_email(sessions, "S-1")
    # Another Record cannot take the reference; a key change (same change set deletes S-1) can.
    with pytest.raises(ValidationFailedError):
        await write(sessions, runtime, person("S-9", held))
    # Nor can a key change copy it twice, or into a Record that exists already.
    await write(sessions, runtime, person("S-7", "other@example.com"))
    for taking in (
        [person("S-8", held), person("S-9", held)],
        [person("S-7", held)],
    ):
        with pytest.raises(ValidationFailedError):
            await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-1"), *taking)
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-1"), person("S-9", held))
    moved = vault_token(await stored_email(sessions, "S-9"))
    assert moved not in (None, vault_token(held))
    assert (await opened(sessions, runtime, moved))[moved] == (  # type: ignore[index]
        VaultState.VALUE,
        "kept@example.com",
    )
    # Erasing the new key erases what the Record held under its earlier key too.
    assert await erase_key(sessions, "S-9") == 3
    token = vault_token(held)
    assert token
    assert (await opened(sessions, runtime, token))[token][0] is VaultState.ERASED


async def test_an_erasure_waits_for_commits_and_commits_wait_for_it(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        assert (await erase_record(session, DOMAIN, SUPPLIER, "S-1")).erased == 1
        # The erasure holds the vault lock: a commit writing the value again waits for it.
        racing = asyncio.create_task(write(sessions, runtime, person("S-1", "ada@example.com")))
        await asyncio.sleep(0.5)
        assert not racing.done()
    await racing
    # It then reads the tombstone: the value is stored erased, never readable again.
    assert await state_of(sessions, runtime, "S-1") is VaultState.ERASED


async def test_token_key_rotation_keeps_tokens_and_finds_old_fingerprints(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"), person("S-2", "Bob@x.io"))
    first = await stored_email(sessions, "S-1")
    rotated = with_vault(
        runtime,
        token_key="n" * 32,
        token_key_id="2",
        previous_token_keys={"1": "t" * 32},
    )
    # The same value written again under the new token key finds its old token: no change.
    assert (await write(sessions, rotated, person("S-1", "ada@example.com"))).no_op
    await write(sessions, rotated, person("S-3", "carol@example.com"))
    async with sessions() as session, session.begin():
        found = await usage(session)
    assert found.fingerprint_keys == {"1/2": 2, "2/2": 1}
    keys = rotated.vault
    assert keys is not None
    report = await rotate(sessions, keys, batch=1, max_rows=100)
    assert (report.fingerprinted, report.unopenable, report.after) == (2, 0, None)
    async with sessions() as session, session.begin():
        assert (await usage(session)).fingerprint_keys == {"2/2": 3}
    # Without the old token key, erasure by value still finds the rotated fingerprints.
    only_new = with_vault(runtime, token_key="n" * 32, token_key_id="2")
    assert await erase_value(sessions, only_new, "bob@x.io") == 1
    assert await stored_email(sessions, "S-1") == first


async def test_encryption_key_rotation_reseals_every_row(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"), person("S-2", "bob@x.io"))
    rotated = with_vault(
        runtime, encryption_key="f" * 32, key_id="k2", previous_keys={"k1": "e" * 32}
    )
    keys = rotated.vault
    assert keys is not None
    first = await rotate(sessions, keys, batch=1, max_rows=1)
    assert (first.rows, first.resealed) == (1, 1)
    assert first.after is not None  # stopped after max_rows: a continuation resumes here
    rest = await rotate(sessions, keys, batch=10, max_rows=10, after=first.after)
    assert (rest.resealed, rest.after) == (1, None)
    async with sessions() as session, session.begin():
        assert (await usage(session)).encryption_keys == {"k2": 2}
    only_new = with_vault(runtime, encryption_key="f" * 32, key_id="k2")
    assert await state_of(sessions, only_new, "S-1") is VaultState.VALUE


async def test_rows_without_a_current_fingerprint_are_made_again_before_erasure(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "Ada@Example.com"))
    keys = runtime.vault
    assert keys is not None
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        # As stored before matching forms: the fingerprint of the canonical value, no key id.
        await session.execute(
            VaultEntry.__table__.update().values(  # type: ignore[attr-defined]
                fingerprint=keys.fingerprints(DOMAIN, SUPPLIER, EMAIL, "Ada@Example.com")[1],
                fingerprint_key=None,
            )
        )
    assert await erase_value(sessions, runtime, "ada@example.com") == 1


async def _domain_scope(session: AsyncSession) -> None:
    await session.execute(text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN})


async def test_a_moved_reference_cannot_be_taken_over_or_claimed_again(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    held = await stored_email(sessions, "S-1")
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-1"), person("S-2", held))
    # Recreating S-1 with its old reference, then moving it once more, would copy the value.
    with pytest.raises(ValidationFailedError) as raised:
        await write(sessions, runtime, person("S-1", held))
    assert [i.code for i in raised.value.issues] == [IssueCode.VALUE_VAULT_REFERENCE]


async def test_erasure_reads_the_lineage_under_the_lock(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    holder = await engine.connect()
    transaction = await holder.begin()
    try:
        # A commit in flight holds the vault lock shared and links S-1 to S-2.
        await holder.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended('lorebase.vault/' || :d, 0))"
            ),
            {"d": DOMAIN},
        )
        await holder.execute(
            text(
                "INSERT INTO vault_lineage (domain_id, kind_id, record_key, previous_key) "
                "VALUES (:d, :k, 'S-2', 'S-1')"
            ),
            {"d": DOMAIN, "k": SUPPLIER},
        )
        await holder.execute(
            text(
                "INSERT INTO vault (domain_id, id, kind_id, attribute_id, record_key, "
                "fingerprint, value_ciphertext, key_id) VALUES "
                "(:d, :t, :k, :a, 'S-2', '\\x00', '\\x00', 'k1')"
            ),
            {"d": DOMAIN, "t": "vlt_" + "2" * 32, "k": SUPPLIER, "a": EMAIL},
        )
        erasing = asyncio.create_task(erase_key(sessions, "S-1"))
        await asyncio.sleep(0.5)
        assert not erasing.done()  # waits for the commit
        await transaction.commit()
    finally:
        await holder.close()
    assert await erasing == 2  # the row the commit added under S-2 too


async def test_erasure_never_stops_at_a_long_lineage(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-0", "ada@example.com"))
    async with sessions() as session, session.begin():
        await _domain_scope(session)
        await session.execute(
            text(
                "INSERT INTO vault_lineage (domain_id, kind_id, record_key, previous_key) "
                "SELECT :d, :k, 'S-' || n, 'S-' || (n - 1) FROM generate_series(1, :n) AS n"
            ),
            {"d": DOMAIN, "k": SUPPLIER, "n": MAX_LINKED_KEYS + 5},
        )
    async with sessions() as session, session.begin():
        await _domain_scope(session)
        erasure = await erase_record(session, DOMAIN, SUPPLIER, f"S-{MAX_LINKED_KEYS + 5}")
    assert (erasure.erased, erasure.linked) == (1, True)


async def test_an_erasure_gives_up_after_its_lock_timeout(
    sessions: Sessions, runtime: StoreRuntime, engine: AsyncEngine
) -> None:
    async with engine.begin() as holder:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended('lorebase.vault/' || :d, 0))"
            ),
            {"d": DOMAIN},
        )
        async with sessions() as session, session.begin():
            await _domain_scope(session)
            with pytest.raises(VaultLockTimeoutError):
                await erase_record(session, DOMAIN, SUPPLIER, "S-1", lock_timeout_ms=200)
            await session.rollback()


async def _moved_to(sessions: Sessions, token: str) -> str | None:
    async with sessions() as session, session.begin():
        await _domain_scope(session)
        found: str | None = await session.scalar(
            select(VaultEntry.moved_to).where(
                VaultEntry.domain_id == DOMAIN, VaultEntry.id == token
            )
        )
        return found


async def test_a_move_replays_on_another_branch_and_follows_chains(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    held = await stored_email(sessions, "S-1")
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "work", created_by="usr_test", mode=BranchMode.COPY, runtime=runtime
        )
    move = (RecordDelete(kind=SUPPLIER, key="S-1"), person("S-2", held))
    await write(sessions, runtime, *move)
    # The same key change on a branch forked before it is the same move: allowed, same token.
    await write(sessions, runtime, *move, branch="work")
    moved = await stored_email(sessions, "S-2")
    assert await stored_email(sessions, "S-2", branch="work") == moved
    # A second rename moves the first token's pointer along.
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-2"), person("S-3", moved))
    first, second = vault_token(held), vault_token(moved)
    assert first
    assert second
    assert await _moved_to(sessions, first) == "S-3"
    assert await _moved_to(sessions, second) == "S-3"


async def test_erased_references_move_freely(sessions: Sessions, runtime: StoreRuntime) -> None:
    await write(sessions, runtime, person("S-5", "ada@example.com"))
    held = await stored_email(sessions, "S-5")
    assert await erase_key(sessions, "S-5") == 1
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-5"), person("S-6", held))
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-6"), person("S-7", held))
    token = vault_token(held)
    assert token
    assert await _moved_to(sessions, token) is None
    assert await state_of(sessions, runtime, "S-7") is VaultState.ERASED

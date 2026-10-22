"""Bundles exported with their vault values: plain values, key changes and tombstones, audit
entries, the vault lock, and an import that stores them again under the target's keys
(design 13.5)."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import vault_token
from lorebase_core.validate import RecordDelete
from lorebase_store import bundle as bundle_module
from lorebase_store.bundle import (
    EXPORT_PLAIN_ACTION,
    EXPORT_PLAIN_STARTED_ACTION,
    IMPORT_VAULT_ACTION,
    BundleError,
    DomainNotEmptyError,
    VaultNotConfiguredError,
    export_domain,
    import_bundle,
    read_manifest,
)
from lorebase_store.repositories import DomainRepository, scope_to_domain
from lorebase_store.repositories.domains import DomainRow
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import BundleSettings, StoreSettings
from lorebase_store.vault import (
    ErasedValues,
    VaultSettings,
    VaultState,
    erase_record,
    open_values,
)
from lorebase_store.vault.store import lock_vault_exclusive

from .support import DOMAIN, SUPPLIER
from .test_bundle import refresh_manifest, target, target_url
from .test_vault import (
    EMAIL,
    VAULT,
    person,
    runtime,
    sessions,
    stored_email,
    with_vault,
    write,
)

pytestmark = pytest.mark.integration

__all__ = ["runtime", "sessions", "target", "target_url"]

Sessions = async_sessionmaker[AsyncSession]
OTHER_KEYS = VaultSettings.model_validate(
    {"token_key": "u" * 32, "encryption_key": "f" * 32, "key_id": "k9"}
)


def plain_runtime(store: StoreSettings, vault: VaultSettings | None) -> StoreRuntime:
    return StoreRuntime.from_settings(
        store.model_copy(update={"bundle": BundleSettings(compression="none")}), vault=vault
    )


async def opened(engine: AsyncEngine, keys: StoreRuntime, *tokens: str) -> dict[str, object]:
    async with async_sessionmaker(engine)() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await open_values(session, DOMAIN, tokens, keys.vault)
    return {t: (v.state, v.value) for t, v in found.items()}


async def audit_entries(
    engine: AsyncEngine, action: str = EXPORT_PLAIN_ACTION
) -> list[tuple[str, str, dict[str, Any]]]:
    async with async_sessionmaker(engine)() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        rows = await session.execute(
            text("SELECT actor, action, details FROM audit_log WHERE action = :a ORDER BY seq"),
            {"a": action},
        )
        return [(r.actor, r.action, r.details) for r in rows]


async def count(engine: AsyncEngine, table: str) -> int:
    async with async_sessionmaker(engine)() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def erase(sessions: Sessions, key: str) -> int:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return (await erase_record(session, DOMAIN, SUPPLIER, key)).erased


async def prepared(sessions: Sessions, runtime: StoreRuntime) -> tuple[str, str]:
    """Two Records with vault values; the first one's value is erased."""
    await write(
        sessions, runtime, person("S-1", "gone@example.com"), person("S-2", "ada@example.com")
    )
    gone = vault_token(await stored_email(sessions, "S-1"))
    kept = vault_token(await stored_email(sessions, "S-2"))
    assert gone
    assert kept
    assert await erase(sessions, "S-1") == 1
    return gone, kept


def rewritten(original: Path, root: Path, section: str, lines: list[dict[str, Any]]) -> Path:
    """A copy of a bundle with other lines in one vault section, rehashed as a forger would."""
    shutil.copytree(original, root)
    (root / f"{section}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    refresh_manifest(root, counts={section: len(lines)})
    return root


def section(root: Path, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / f"{name}.jsonl").read_text().splitlines()]


async def test_values_travel_audited_and_open_under_the_target_keys(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    source_runtime = plain_runtime(store, VAULT)
    gone, kept = await prepared(sessions, source_runtime)

    archive = tmp_path / "plain.tar"
    report = await export_domain(
        engine,
        DOMAIN,
        archive,
        runtime=source_runtime,
        include_vault_values=True,
        actor="usr_operator",
    )
    assert (report.vault_values, report.vault_erased, report.vault_tombstones) == (1, 1, 1)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert [e[0] for e in await audit_entries(engine, EXPORT_PLAIN_STARTED_ACTION)] == [
        "usr_operator"
    ]
    entries = await audit_entries(engine)
    assert len(entries) == 1
    actor, _, details = entries[0]
    assert actor == "usr_operator"
    assert (details["values"], details["erased"], details["tombstones"]) == (1, 1, 1)
    assert "main" in details["heads"]
    assert "ada@example.com" not in json.dumps(details)

    # Same token key id, other secrets: values are sealed again under the target's keys.
    target_runtime = plain_runtime(store, OTHER_KEYS)
    imported = await import_bundle(target, archive, runtime=target_runtime)
    assert (imported.vault_values, imported.vault_erased) == (1, 1)
    assert await opened(target, target_runtime, gone, kept) == {
        gone: (VaultState.ERASED, None),
        kept: (VaultState.VALUE, "ada@example.com"),
    }
    assert await count(target, "vault_tombstones") == 1


async def test_bundle_directory_lists_the_vault_files_and_stays_private(
    engine: AsyncEngine, sessions: Sessions, store: StoreSettings, tmp_path: Path
) -> None:
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    bundle = tmp_path / "plain"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    manifest = read_manifest(bundle)
    assert manifest["counts"]["vault"] == 2
    assert manifest["counts"]["vault_tombstones"] == 1
    assert manifest["counts"]["vault_lineage"] == 0
    for name in ("vault", "vault_lineage", "vault_tombstones"):
        assert f"{name}.jsonl" in manifest["files"]
        assert stat.S_IMODE((bundle / f"{name}.jsonl").stat().st_mode) == 0o600
    lines = section(bundle, "vault")
    assert sorted(("value" in line, line.get("erased", False)) for line in lines) == [
        (False, True),
        (True, False),
    ]

    default = tmp_path / "default"
    await export_domain(engine, DOMAIN, default, runtime=runtime)
    assert not (default / "vault.jsonl").exists()
    assert "vault" not in read_manifest(default)["counts"]
    assert len(await audit_entries(engine)) == 1


async def test_export_without_keys_is_refused_and_writes_nothing(
    engine: AsyncEngine, sessions: Sessions, store: StoreSettings, tmp_path: Path
) -> None:
    with pytest.raises(VaultNotConfiguredError):
        await export_domain(
            engine,
            DOMAIN,
            tmp_path / "plain",
            runtime=plain_runtime(store, None),
            include_vault_values=True,
        )
    assert not (tmp_path / "plain").exists()
    assert await audit_entries(engine, EXPORT_PLAIN_STARTED_ACTION) == []


async def test_forged_vault_entries_are_refused(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    original = tmp_path / "original"
    await export_domain(engine, DOMAIN, original, runtime=runtime, include_vault_values=True)
    lines = section(original, "vault")
    valued = next(i for i, line in enumerate(lines) if "value" in line)

    def forged(name: str, changed: list[dict[str, Any]]) -> Path:
        return rewritten(original, tmp_path / name, "vault", changed)

    rebound = [dict(line) for line in lines]
    rebound[valued]["record_key"] = "S-404"
    with pytest.raises(BundleError, match="neither its binding nor linked"):
        await import_bundle(target, forged("rebound", rebound), runtime=runtime)

    unknown = [*lines, {**lines[valued], "token": "vlt_" + "0" * 32}]
    with pytest.raises(BundleError, match="no Record object"):
        await import_bundle(target, forged("unknown", unknown), runtime=runtime)

    twice = [*lines, lines[valued]]
    with pytest.raises(BundleError, match="appears twice"):
        await import_bundle(target, forged("twice", twice), runtime=runtime)

    referenced = [dict(line) for line in lines]
    referenced[valued]["value"] = {"$vault": "vlt_" + "1" * 32}
    with pytest.raises(BundleError, match="vault reference"):
        await import_bundle(target, forged("referenced", referenced), runtime=runtime)

    # A value of the wrong type for the Attribute (L4).
    mistyped = [dict(line) for line in lines]
    mistyped[valued]["value"] = 12345
    with pytest.raises(BundleError, match=r"vault line \d+: vlt_"):
        await import_bundle(target, forged("mistyped", mistyped), runtime=runtime)

    with pytest.raises(VaultNotConfiguredError):
        await import_bundle(target, original, runtime=plain_runtime(store, None))
    assert await count(target, "vault") == 0


async def test_a_key_change_of_an_erased_value_travels(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    """M4: erase, then change the key: the old and the new Record both hold the token."""
    runtime = plain_runtime(store, VAULT)
    await write(sessions, runtime, person("S-1", "gone@example.com"))
    assert await erase(sessions, "S-1") == 1
    held = await stored_email(sessions, "S-1")
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-1"), person("S-9", held))
    assert await stored_email(sessions, "S-9") == held
    token = vault_token(held)
    assert token

    bundle = tmp_path / "renamed"
    report = await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    assert (report.vault_erased, report.vault_lineage) == (1, 1)
    entry = {
        "token": token,
        "kind": SUPPLIER,
        "attribute": EMAIL,
        "record_key": "S-1",
        "erased": True,
    }
    # An erased reference carries no value: the key change does not mark it moved.
    assert section(bundle, "vault") == [entry]

    # Without the key change the second holder is refused.
    unlinked = rewritten(bundle, tmp_path / "unlinked", "vault_lineage", [])
    with pytest.raises(BundleError, match="'S-9'"):
        await import_bundle(target, unlinked, runtime=runtime)
    # L3: a moved_to no key change leads to is refused.
    elsewhere = rewritten(bundle, tmp_path / "elsewhere", "vault", [{**entry, "moved_to": "S-1"}])
    with pytest.raises(BundleError, match="moved_to"):
        await import_bundle(target, elsewhere, runtime=runtime)
    astray = rewritten(bundle, tmp_path / "astray", "vault", [{**entry, "moved_to": "S-7"}])
    with pytest.raises(BundleError, match="no key change of the bundle leads there"):
        await import_bundle(target, astray, runtime=runtime)
    # L4: a key change must link Records the bundle holds.
    lineage = section(bundle, "vault_lineage")
    invented = [*lineage, {"kind": SUPPLIER, "record_key": "S-404", "previous_key": "S-9"}]
    forged = rewritten(bundle, tmp_path / "invented", "vault_lineage", invented)
    with pytest.raises(BundleError, match="no Record 'S-404'"):
        await import_bundle(target, forged, runtime=runtime)

    imported = await import_bundle(target, bundle, runtime=runtime, actor="usr_restore")
    assert (imported.vault_erased, imported.vault_lineage) == (1, 1)
    assert await opened(target, runtime, token) == {token: (VaultState.ERASED, None)}
    assert await count(target, "vault_lineage") == 1
    assert await moved_to(target, token) is None
    [(actor, _, details)] = await audit_entries(target, IMPORT_VAULT_ACTION)
    assert actor == "usr_restore"
    assert details == {
        "values": 0,
        "erased": 1,
        "tombstones": 1,
        "lineage_edges": 1,
        "oversized_lineage_groups": 0,
    }


async def test_oversized_lineage_groups_are_counted_in_the_import_audit(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L4: a group of linked keys larger than MAX_LINKED_KEYS is flagged in the audit entry."""
    runtime = plain_runtime(store, VAULT)
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    held = await stored_email(sessions, "S-1")
    await write(sessions, runtime, RecordDelete(kind=SUPPLIER, key="S-1"), person("S-2", held))
    bundle = tmp_path / "renamed"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    monkeypatch.setattr(bundle_module, "MAX_LINKED_KEYS", 1)
    await import_bundle(target, bundle, runtime=runtime)
    [(_, _, details)] = await audit_entries(target, IMPORT_VAULT_ACTION)
    assert (details["lineage_edges"], details["oversized_lineage_groups"]) == (1, 1)


async def test_legacy_tombstones_travel_under_the_current_token_key(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    """L5: a tombstone without fingerprint_key is exported as `<token_key_id>/1`, said in the
    manifest, and imported under the token key rule; one still without a key is refused."""
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(text("UPDATE vault_tombstones SET fingerprint_key = NULL"))
    bundle = tmp_path / "legacy"
    report = await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    assert report.vault_legacy_tombstones == 1
    [tombstone] = section(bundle, "vault_tombstones")
    assert tombstone["fingerprint_key"] == f"{VAULT.token_key_id}/1"
    notes = read_manifest(bundle)["vault_notes"]
    assert (notes["legacy_tombstones"], notes["legacy_fingerprint_key"]) == (1, "1/1")

    keyless = rewritten(
        bundle, tmp_path / "keyless", "vault_tombstones", [{**tombstone, "fingerprint_key": None}]
    )
    with pytest.raises(BundleError, match="does not say which token key"):
        await import_bundle(target, keyless, runtime=runtime)
    with pytest.raises(BundleError, match="token key '1/1'"):
        await import_bundle(target, bundle, runtime=with_vault(runtime, token_key_id="9"))
    await import_bundle(target, bundle, runtime=runtime)
    assert await count(target, "vault_tombstones") == 1


async def test_export_lets_the_vault_lock_go_once_its_snapshot_exists(
    engine: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L6: while the export writes files, an erasure can take the vault lock."""
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    free: list[bool] = []
    export_nodes = bundle_module._export_nodes

    async def probing(*args: Any) -> int:
        async with engine.connect() as probe:
            free.append(
                bool(
                    (
                        await probe.execute(
                            text(
                                "SELECT pg_try_advisory_xact_lock("
                                "hashtextextended('lorebase.vault/' || :d, 0))"
                            ),
                            {"d": DOMAIN},
                        )
                    ).scalar_one()
                )
            )
        return await export_nodes(*args)

    monkeypatch.setattr(bundle_module, "_export_nodes", probing)
    await export_domain(
        engine, DOMAIN, tmp_path / "plain", runtime=runtime, include_vault_values=True
    )
    assert free == [True]


async def moved_to(engine: AsyncEngine, token: str) -> str | None:
    async with async_sessionmaker(engine)() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        found = await session.execute(
            text("SELECT moved_to FROM vault WHERE id = :t"), {"t": token}
        )
        value = found.scalar_one()
        return None if value is None else str(value)


async def test_tombstones_need_the_token_key_they_were_made_under(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    """M5: tombstones are exported and only imported where their token key id exists."""
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    bundle = tmp_path / "plain"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    [tombstone] = section(bundle, "vault_tombstones")
    assert tombstone["record_key"] == "S-1"
    assert tombstone["fingerprint_key"].startswith(f"{VAULT.token_key_id}/")

    elsewhere = with_vault(runtime, token_key_id="9")
    with pytest.raises(BundleError, match="token key '1/2'"):
        await import_bundle(target, bundle, runtime=elsewhere)
    assert await count(target, "vault_tombstones") == 0

    rotated = with_vault(runtime, token_key_id="9", previous_token_keys={"1": VAULT.token_key})
    await import_bundle(target, bundle, runtime=rotated)
    assert await count(target, "vault_tombstones") == 1


@pytest.mark.parametrize("policy", list(ErasedValues))
async def test_imported_values_meet_the_tombstones(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
    policy: ErasedValues,
) -> None:
    """M6: a value written again after its erasure (allowed on the source) is handled on import
    as the target's `vault.erased_values` says."""
    runtime = plain_runtime(store, VAULT)
    await write(sessions, runtime, person("S-1", "ada@example.com"))
    assert await erase(sessions, "S-1") == 1
    await write(
        sessions,
        with_vault(runtime, erased_values=ErasedValues.ALLOW),
        person("S-1", "ada@example.com"),
    )
    again = vault_token(await stored_email(sessions, "S-1"))
    assert again
    bundle = tmp_path / "again"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)

    importing = with_vault(runtime, erased_values=policy)
    if policy is ErasedValues.REFUSE:
        with pytest.raises(BundleError, match="erased"):
            await import_bundle(target, bundle, runtime=importing)
        assert await count(target, "vault") == 0
        return
    imported = await import_bundle(target, bundle, runtime=importing)
    state = (await opened(target, importing, again))[again]
    if policy is ErasedValues.ERASE:
        assert state == (VaultState.ERASED, None)
        assert imported.vault_values == 0
    else:
        assert state == (VaultState.VALUE, "ada@example.com")


async def test_a_domain_holding_vault_rows_is_not_empty(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    """M6: leftover vault rows, tombstones or key changes make the target Domain non-empty."""
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    bundle = tmp_path / "plain"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    data = json.loads((bundle / "domain.json").read_text())
    async with async_sessionmaker(target)() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await DomainRepository(session).create(
            DomainRow(**{**data, "created_at": datetime.fromisoformat(data["created_at"])})
        )
        await session.execute(
            text(
                "INSERT INTO vault_lineage (domain_id, kind_id, record_key, previous_key) "
                "VALUES (:d, :k, 'S-5', 'S-4')"
            ),
            {"d": DOMAIN, "k": SUPPLIER},
        )
    with pytest.raises(DomainNotEmptyError, match="1 vault key changes"):
        await import_bundle(target, bundle, runtime=runtime)


async def test_export_waits_for_an_erasure_and_audits_its_start(
    engine: AsyncEngine, sessions: Sessions, store: StoreSettings, tmp_path: Path
) -> None:
    """L3: the snapshot is taken under the shared vault lock, after the start is audited."""
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    bundle = tmp_path / "plain"
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await lock_vault_exclusive(session, DOMAIN)
        exporting = asyncio.create_task(
            export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
        )
        await asyncio.sleep(0.5)
        assert not exporting.done()
        assert len(await audit_entries(engine, EXPORT_PLAIN_STARTED_ACTION)) == 1
        assert await audit_entries(engine) == []
        # The erasure in flight commits before the export reads the vault.
        assert (await erase_record(session, DOMAIN, SUPPLIER, "S-2")).erased == 1
    report = await exporting
    assert (report.vault_values, report.vault_erased) == (0, 2)
    assert all("value" not in line for line in section(bundle, "vault"))
    assert len(await audit_entries(engine)) == 1

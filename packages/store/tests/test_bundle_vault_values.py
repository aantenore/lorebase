"""Bundles exported with their vault values: plain values, audit entry, and an import that
stores them again under the target's keys (design 13.5)."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import vault_token
from lorebase_store.bundle import (
    EXPORT_PLAIN_ACTION,
    BundleError,
    VaultNotConfiguredError,
    export_domain,
    import_bundle,
    read_manifest,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import BundleSettings, StoreSettings
from lorebase_store.vault import VaultSettings, VaultState, erase_record, open_values

from .support import DOMAIN, SUPPLIER
from .test_bundle import refresh_manifest, target, target_url
from .test_vault import VAULT, person, runtime, sessions, stored_email, write

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
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        found = await open_values(session, DOMAIN, tokens, keys.vault)
    return {t: (v.state, v.value) for t, v in found.items()}


async def audit_entries(engine: AsyncEngine) -> list[tuple[str, str, dict[str, object]]]:
    async with async_sessionmaker(engine)() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        rows = await session.execute(
            text("SELECT actor, action, details FROM audit_log WHERE action = :a ORDER BY seq"),
            {"a": EXPORT_PLAIN_ACTION},
        )
        return [(r.actor, r.action, r.details) for r in rows]


async def prepared(sessions: Sessions, runtime: StoreRuntime) -> tuple[str, str]:
    """Two Records with vault values; the first one's value is erased."""
    await write(
        sessions, runtime, person("S-1", "gone@example.com"), person("S-2", "ada@example.com")
    )
    gone = vault_token(await stored_email(sessions, "S-1"))
    kept = vault_token(await stored_email(sessions, "S-2"))
    assert gone
    assert kept
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('lorebase.domain_ids', :d, true)"), {"d": DOMAIN}
        )
        assert (await erase_record(session, DOMAIN, SUPPLIER, "S-1")).erased == 1
    return gone, kept


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
    assert (report.vault_values, report.vault_erased) == (1, 1)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    entries = await audit_entries(engine)
    assert len(entries) == 1
    actor, _, details = entries[0]
    assert actor == "usr_operator"
    assert details["values"] == 1
    assert details["erased"] == 1
    assert "main" in details["heads"]  # type: ignore[operator]
    assert "ada@example.com" not in json.dumps(details)

    target_runtime = plain_runtime(store, OTHER_KEYS)
    imported = await import_bundle(target, archive, runtime=target_runtime)
    assert (imported.vault_values, imported.vault_erased) == (1, 1)
    assert await opened(target, target_runtime, gone, kept) == {
        gone: (VaultState.ERASED, None),
        kept: (VaultState.VALUE, "ada@example.com"),
    }


async def test_bundle_directory_lists_the_vault_file_and_stays_private(
    engine: AsyncEngine, sessions: Sessions, store: StoreSettings, tmp_path: Path
) -> None:
    runtime = plain_runtime(store, VAULT)
    await prepared(sessions, runtime)
    bundle = tmp_path / "plain"
    await export_domain(engine, DOMAIN, bundle, runtime=runtime, include_vault_values=True)
    manifest = read_manifest(bundle)
    assert manifest["counts"]["vault"] == 2
    assert "vault.jsonl" in manifest["files"]
    assert stat.S_IMODE((bundle / "vault.jsonl").stat().st_mode) == 0o600
    lines = [json.loads(line) for line in (bundle / "vault.jsonl").read_text().splitlines()]
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
    assert await audit_entries(engine) == []


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
    lines = [json.loads(line) for line in (original / "vault.jsonl").read_text().splitlines()]
    valued = next(i for i, line in enumerate(lines) if "value" in line)

    def forged(name: str, changed: list[dict[str, object]]) -> Path:
        root = tmp_path / name
        shutil.copytree(original, root)
        (root / "vault.jsonl").write_text("".join(json.dumps(line) + "\n" for line in changed))
        refresh_manifest(root, counts={"vault": len(changed)})
        return root

    rebound = [dict(line) for line in lines]
    rebound[valued]["record_key"] = "S-404"
    with pytest.raises(BundleError, match="no Record object"):
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

    with pytest.raises(VaultNotConfiguredError):
        await import_bundle(target, original, runtime=plain_runtime(store, None))

"""The `supply` example for extraction tests: Records without most Links, the four example
documents stored as uploads, and helpers to read items back."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Link, Schema
from lorebase_core.revision import Origin
from lorebase_core.validate import DataChange, LinkUpsert, RecordUpsert
from lorebase_scribe.extraction.parsing import MARKDOWN
from lorebase_store.blobs import PostgresBlobStore
from lorebase_store.commit import commit
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    ItemRow,
    ProposalItemRepository,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

from .support import AUTHOR, DOMAIN, EXAMPLE, kind_changes, load_domain, rows

DOCUMENTS = EXAMPLE / "documents"
NEW_CONTRACT = "C-3025"
"""Left out of the seeded data so that the contract document proposes it."""


async def seed_supply(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Callable[[], datetime],
    schema: Schema,
) -> None:
    """Suppliers, Parts, Plants and Contracts (without C-3025) with their `with_supplier`
    Links; no `supplied_by` or `stocked_at` Links, so documents can propose them."""
    await load_domain(
        sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv", "Part": "part.csv"}
    )
    plants = kind_changes(schema, "Plant", "plant.csv")
    contracts = [
        c
        for c in kind_changes(schema, "Contract", "contract.csv")
        if isinstance(c, RecordUpsert) and c.record.key != NEW_CONTRACT
    ]
    found = schema.relation_by_api_name("with_supplier")
    assert found is not None
    relation = found[0]
    links: list[DataChange] = [
        LinkUpsert(link=Link(relation=relation.id, from_key=r["from_key"], to_key=r["to_key"]))
        for r in rows("with_supplier.csv")
        if r["from_key"] != NEW_CONTRACT
    ]
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [*plants, *contracts, *links],
            author=AUTHOR,
            message="Import plants and contracts",
            origin=Origin(type="import"),
            runtime=runtime,
            clock=clock,
        )


async def store_document(
    sessions: async_sessionmaker[AsyncSession], path: Path, *, media_type: str = MARKDOWN
) -> str:
    """Store a file as an uploaded document (what Keeper's intake does); returns its id."""
    data = await asyncio.to_thread(path.read_bytes)
    digest = hashlib.sha256(data).digest()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        ref = await PostgresBlobStore().put(session, DOMAIN, data)
        row, _created = await DocumentRepository(session, DOMAIN).add(
            DocumentRow(
                id=f"doc_{digest.hex()[:26]}",
                filename=path.name,
                media_type=media_type,
                sha256=digest,
                size=len(data),
                blob_ref=ref,
                created_by="usr_loader",
            )
        )
    return row.id


async def proposal_items(
    sessions: async_sessionmaker[AsyncSession], proposal_id: str
) -> list[ItemRow]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return await ProposalItemRepository(session, DOMAIN).all(proposal_id)

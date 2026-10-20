"""A Keeper on Postgres (from the shared harness) with the `supply` example loaded."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from keeper_harness import RunningKeeper
from supply_example import (
    DOMAIN,
    KIND_FILES,
    LINK_FILES,
    WORK,
    Loaded,
    attribute_types,
    read_rows,
    stripped_schema_yaml,
)

from lorebase_client import AsyncLorebaseClient


@pytest.fixture
async def client(in_process_keeper: RunningKeeper) -> AsyncIterator[AsyncLorebaseClient]:
    """An administrator's client with the ETag cache on."""
    async with AsyncLorebaseClient(
        in_process_keeper.url,
        api_key=in_process_keeper.admin_key,
        transport=in_process_keeper.transport,
        cache=True,
    ) as admin:
        yield admin


@pytest.fixture
async def supply(client: AsyncLorebaseClient) -> Loaded:
    """Domain `supply`, branch `work` with the stripped schema and all example rows."""
    created = await client.domains.create(DOMAIN, display_name="Supply chain")
    assert created.head
    await client.branches.create(DOMAIN, WORK)
    applied = await client.schema.apply(
        DOMAIN, WORK, yaml=stripped_schema_yaml(), message="Apply the supply schema"
    )
    assert applied.revision is not None
    loaded = Loaded(base_head=applied.head, schema_revision=applied.revision)
    schema = (await client.schema.get(DOMAIN, branch=WORK)).schema_
    for kind, file in KIND_FILES.items():
        rows = read_rows(file, attribute_types(schema, "kinds", kind), links=False)
        result = await client.operations.import_records(DOMAIN, kind, rows, branch=WORK)
        assert result.status == "committed", (kind, result.violations)
        loaded.import_revisions.extend(result.revisions)
        loaded.counts[kind] = len(rows)
    for relation, file in LINK_FILES.items():
        rows = read_rows(file, attribute_types(schema, "relations", relation), links=True)
        result = await client.operations.import_links(DOMAIN, relation, rows, branch=WORK)
        assert result.status == "committed", (relation, result.violations)
        loaded.import_revisions.extend(result.revisions)
        loaded.counts[relation] = len(rows)
    return loaded

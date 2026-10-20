from __future__ import annotations

from lorebase_store.models import metadata

DESIGN_TABLES = {
    # 7.1 versioning core
    "domains",
    "objects",
    "tree_nodes",
    "revisions",
    "revision_parents",
    "changesets",
    "provenance",
    "refs",
    "ref_log",
    # 7.2 materialized heads
    "mat_schema",
    "mat_records",
    "mat_links",
    "mat_history",
    "embeddings",
    # 7.3 proposals and merges
    "proposals",
    "proposal_reviews",
    "proposal_items",
    "merge_attempts",
    "conflicts",
    "conflict_resolutions",
    # 7.4 governance, ingestion, platform
    "principals",
    "groups",
    "group_members",
    "grants",
    "api_keys",
    "operation_invocations",
    "audit_log",
    "outbox",
    "jobs",
    "source_runs",
    "documents",
    "document_chunks",
    "extraction_jobs",
    "extraction_cache",
    "vault",
}
HASH_PARTITIONED = {"objects", "tree_nodes", "mat_records", "mat_links"}


def test_every_design_table_is_modelled() -> None:
    assert set(metadata.tables) == DESIGN_TABLES


def test_hash_partitioned_tables() -> None:
    partitioned = {
        name
        for name, table in metadata.tables.items()
        if table.dialect_options["postgresql"]["partition_by"]
    }
    assert partitioned == HASH_PARTITIONED
    for name in HASH_PARTITIONED:
        assert metadata.tables[name].primary_key.columns.keys()[0] == "domain_id"


def test_domain_scoped_tables_lead_with_domain_id() -> None:
    for table in metadata.tables.values():
        if "domain_id" in table.c and table.c.domain_id.primary_key:
            assert table.primary_key.columns.keys()[0] == "domain_id", table.name


def test_named_indexes_and_constraints() -> None:
    indexes = {index.name for table in metadata.tables.values() for index in table.indexes}
    assert {
        "ix_mat_records_search",
        "ix_mat_records_values",
        "ix_mat_records_title",
        "ix_mat_links_reverse",
        "ix_revision_parents_domain_id_parent_id",
        "uq_operation_invocations_idempotency",
        "ix_jobs_claimable",
        "ix_outbox_undelivered",
    } <= indexes

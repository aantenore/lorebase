from __future__ import annotations

from lorebase_store.migrations import DYNAMIC_INDEX_PREFIXES, head_revisions, include_object
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
    "mat_lexicon",
    "mat_lineage_maps",
    "storage_tasks",
    "mat_history",
    "embeddings",
    "embedding_cursors",
    "branch_materializations",
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
    "api_key_usage",
    "web_sessions",
    "operation_invocations",
    "operation_deferred_events",
    "audit_log",
    "audit_read_log",
    "rate_limit_buckets",
    "agent_trace_steps",
    "agent_trace_sessions",
    "agent_trace_session_domains",
    "outbox",
    "outbox_consumers",
    "outbox_pending",
    "jobs",
    "source_runs",
    "source_written",
    "source_written_pending",
    "documents",
    "document_blobs",
    "document_chunks",
    "extraction_jobs",
    "extraction_cache",
    "schema_suggestions",
    "vault",
    "vault_tombstones",
    "inference_endpoint_modes",
    "vault_lineage",
    "installation_secrets",
    # 10.6 reconciliation
    "reconciliation_candidates",
    # 10.5 webhooks
    "webhooks",
    "webhook_deliveries",
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
        "ix_mat_records_branch_search",
        "ix_mat_records_values",
        "ix_mat_lexicon_branch_word",
        "ix_mat_links_reverse",
        "ix_revision_parents_domain_id_parent_id",
        "uq_operation_invocations_idempotency",
        "ix_jobs_claimable",
        "ix_outbox_created_at",
        "ix_outbox_pending_event_id",
    } <= indexes
    assert not any(str(name).startswith(DYNAMIC_INDEX_PREFIXES) for name in indexes)


def test_comparison_filter_skips_partitions_and_runtime_indexes() -> None:
    assert not include_object(None, "objects_p3", "table", True, None)
    assert include_object(None, "objects", "table", False, None)
    assert not include_object(None, "embeddings_hnsw_384", "index", True, None)
    assert include_object(None, "ix_jobs_claimable", "index", True, None)


def test_head_revision_is_the_latest_migration() -> None:
    assert head_revisions() == ("0041",)

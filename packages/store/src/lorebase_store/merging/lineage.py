"""Provenance of the source side of a merge, read from its materialized head."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from lorebase_store.layout import BranchLayout, load_layout_sync
from lorebase_store.materialize.rows import PartitionType, parse_partition, split_link_key


class BranchLineage:
    """Core `ProvenanceLoader` over the materialized head of one branch.

    The merge asks for the provenance of elements of the source head; the materialized rows of
    the source branch hold their Lineage (Records and Links) and the last change of each
    schema element (history). The Revision argument is not consulted: it is the branch head
    the merge was started from, read in the same transaction. An overlay source is read
    through its resolved rows.
    """

    def __init__(self, session: Session, domain_id: str, branch: str) -> None:
        self._session = session
        self._domain = domain_id
        self._branch = branch
        self._layout: BranchLayout | None = None

    def _resolved(self) -> BranchLayout:
        if self._layout is None:
            self._layout = load_layout_sync(self._session, self._domain, self._branch)
        return self._layout

    def __call__(self, revision: bytes, partition: str, key: str) -> Mapping[str, str]:
        parsed = parse_partition(partition)
        layout = self._resolved()
        if parsed.type is PartitionType.KIND:
            records = layout.records()
            query = select(records.c.lineage).where(
                records.c.domain_id == self._domain,
                records.c.branch == self._branch,
                records.c.kind_id == parsed.element_id,
                records.c.key == key,
            )
        elif parsed.type is PartitionType.RELATION:
            from_key, to_key = split_link_key(key)
            links = layout.links()
            query = select(links.c.lineage).where(
                links.c.domain_id == self._domain,
                links.c.branch == self._branch,
                links.c.relation_id == parsed.element_id,
                links.c.from_key == from_key,
                links.c.to_key == to_key,
            )
        else:
            history = layout.history()
            query = (
                select(history.c.provenance)
                .where(
                    history.c.domain_id == self._domain,
                    history.c.branch == self._branch,
                    history.c.partition == partition,
                    history.c.element_key == key,
                )
                .order_by(history.c.at.desc())
                .limit(1)
            )
        found = self._session.execute(query).scalar_one_or_none()
        return {str(k): str(v) for k, v in (found or {}).items()}

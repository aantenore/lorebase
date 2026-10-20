"""Provenance of the source side of a merge, read from its materialized head."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from lorebase_store.materialize.rows import PartitionType, parse_partition, split_link_key
from lorebase_store.repositories.materialized import MAT_HISTORY, MAT_LINKS, MAT_RECORDS


class BranchLineage:
    """Core `ProvenanceLoader` over the materialized head of one branch.

    The merge asks for the provenance of elements of the source head; the materialized rows of
    the source branch hold their Lineage (Records and Links) and the last change of each
    schema element (history). The Revision argument is not consulted: it is the branch head
    the merge was started from, read in the same transaction.
    """

    def __init__(self, session: Session, domain_id: str, branch: str) -> None:
        self._session = session
        self._domain = domain_id
        self._branch = branch

    def __call__(self, revision: bytes, partition: str, key: str) -> Mapping[str, str]:
        parsed = parse_partition(partition)
        if parsed.type is PartitionType.KIND:
            query = select(MAT_RECORDS.c.lineage).where(
                MAT_RECORDS.c.domain_id == self._domain,
                MAT_RECORDS.c.branch == self._branch,
                MAT_RECORDS.c.kind_id == parsed.element_id,
                MAT_RECORDS.c.key == key,
            )
        elif parsed.type is PartitionType.RELATION:
            from_key, to_key = split_link_key(key)
            query = select(MAT_LINKS.c.lineage).where(
                MAT_LINKS.c.domain_id == self._domain,
                MAT_LINKS.c.branch == self._branch,
                MAT_LINKS.c.relation_id == parsed.element_id,
                MAT_LINKS.c.from_key == from_key,
                MAT_LINKS.c.to_key == to_key,
            )
        else:
            query = (
                select(MAT_HISTORY.c.provenance)
                .where(
                    MAT_HISTORY.c.domain_id == self._domain,
                    MAT_HISTORY.c.branch == self._branch,
                    MAT_HISTORY.c.partition == partition,
                    MAT_HISTORY.c.element_key == key,
                )
                .order_by(MAT_HISTORY.c.at.desc())
                .limit(1)
            )
        found = self._session.execute(query).scalar_one_or_none()
        return {str(k): str(v) for k, v in (found or {}).items()}

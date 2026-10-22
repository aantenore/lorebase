"""Provenance of the sides of a merge, read from their materialized heads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from lorebase_core.merge import ChangeStamp
from lorebase_store.layout import BranchLayout, load_layout_sync
from lorebase_store.materialize.rows import PartitionType, parse_partition, split_link_key
from lorebase_store.repositories.provenance import PROVENANCE


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


class MergeLineage:
    """Core `LineageLoader`: the change stamps of both sides of a merge.

    `branches` maps the head Revision of each merged branch to its name; the Lineage of an
    element is read from that branch's materialized head (see `BranchLineage`) and its
    provenance ids are resolved to stamps (origin labels and time) from the provenance table.
    Provenance rows are read once per merge.
    """

    def __init__(self, session: Session, domain_id: str, branches: Mapping[bytes, str]) -> None:
        self._session = session
        self._domain = domain_id
        self._branches = {
            revision: BranchLineage(session, domain_id, name) for revision, name in branches.items()
        }
        self._stamps: dict[str, ChangeStamp | None] = {}

    def __call__(self, revision: bytes, partition: str, key: str) -> Mapping[str, ChangeStamp]:
        branch = self._branches.get(revision)
        if branch is None:
            return {}
        ids = branch(revision, partition, key)
        self._load(ids.values())
        found: dict[str, ChangeStamp] = {}
        for attribute, provenance_id in ids.items():
            stamp = self._stamps.get(provenance_id)
            if stamp is not None:
                found[attribute] = stamp
        return found

    def _load(self, ids: Iterable[str]) -> None:
        missing = sorted({i for i in ids if i not in self._stamps})
        if not missing:
            return
        rows = self._session.execute(
            select(
                PROVENANCE.c.id,
                PROVENANCE.c.origin_type,
                PROVENANCE.c.origin_ref,
                PROVENANCE.c.actor,
                PROVENANCE.c.at,
            ).where(PROVENANCE.c.domain_id == self._domain, PROVENANCE.c.id.in_(missing))
        )
        for row in rows:
            self._stamps[row.id] = ChangeStamp.from_stored(
                row.id, row.origin_type, row.origin_ref or {}, row.actor, row.at
            )
        for provenance_id in missing:
            self._stamps.setdefault(provenance_id, None)

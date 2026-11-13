"""A fast-forward result committed as returned keeps the history connected."""

from __future__ import annotations

from lorebase_core.merge import ConflictKind, MergeResult

from .support import Edit, Repo, attempt, base_records, base_schema, merged, supplier


def _fork() -> tuple[Repo, bytes, bytes, bytes]:
    repo = Repo()
    base = repo.commit("base", Edit(schema=base_schema(), put=list(base_records())))
    source = repo.commit("source", Edit().upsert(supplier("S-1", vat="IT001", rating="4")), base)
    model = repo.commit("model", Edit().upsert(supplier("S-1", vat="IT001", rating="5")), base)
    return repo, base, source, model


def test_fast_forward_commit_stays_connected_to_the_target() -> None:
    repo, base, source, model = _fork()
    forward = merged(repo.merge(base, source))
    assert forward.result is MergeResult.FAST_FORWARD
    assert forward.root == repo.roots[source]
    assert forward.parents == (base,)

    head = repo.add("main", forward.root, *forward.parents)
    result = attempt(repo.merge(head, model))
    assert result.base == base
    assert [c.kind for c in result.conflicts] == [ConflictKind.ATTRIBUTE_VALUE]


def test_up_to_date_result_keeps_the_target_as_parent() -> None:
    repo, base, source, _ = _fork()
    same = merged(repo.merge(source, base))
    assert same.result is MergeResult.UP_TO_DATE
    assert same.parents == (source,)

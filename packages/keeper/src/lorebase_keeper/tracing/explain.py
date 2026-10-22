"""Query plan summaries of traced requests (`X-Lorebase-Trace: debug`).

When a traced request asks for plans and the caller holds trace.explain in the Domain, the
query and search compilers pass their main statement to `explain` before running it: Postgres
plans it (`EXPLAIN (FORMAT JSON)`, never `ANALYZE`, so nothing runs twice) inside a savepoint,
and a summary is kept: node types, indexes and relations scanned, the estimated rows and
cost. A failed plan is skipped; it never fails the read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement

from lorebase_keeper.tracing.notes import MAX_PLANS, current

logger = logging.getLogger("lorebase.keeper.trace")

MAX_SCANS = 20


class _Explain(Executable, ClauseElement):
    """`EXPLAIN (FORMAT JSON) <statement>`."""

    inherit_cache = False

    def __init__(self, statement: ClauseElement) -> None:
        self.statement = statement


@compiles(_Explain)
def _compile_explain(element: _Explain, compiler: SQLCompiler, **kw: Any) -> str:
    return "EXPLAIN (FORMAT JSON) " + compiler.process(element.statement, **kw)


def _nodes(plan: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield plan
    for child in plan.get("Plans") or ():
        if isinstance(child, Mapping):
            yield from _nodes(child)


def summarize(label: str, document: Any) -> dict[str, Any]:
    """The summary kept of an `EXPLAIN (FORMAT JSON)` document."""
    if isinstance(document, str):
        document = json.loads(document)
    root = document[0]["Plan"] if isinstance(document, list) and document else {}
    node_types: list[str] = []
    indexes: list[str] = []
    scans: list[dict[str, Any]] = []
    for node in _nodes(root):
        kind = str(node.get("Node Type", ""))
        if kind and kind not in node_types:
            node_types.append(kind)
        index = node.get("Index Name")
        if isinstance(index, str) and index not in indexes:
            indexes.append(index)
        relation = node.get("Relation Name")
        if (relation or index) and len(scans) < MAX_SCANS:
            scan = {"node": kind, "relation": relation, "index": index}
            scans.append({k: v for k, v in scan.items() if v is not None})
    return {
        "label": label,
        "node": root.get("Node Type"),
        "rows": root.get("Plan Rows"),
        "cost": root.get("Total Cost"),
        "node_types": node_types,
        "indexes": indexes,
        "scans": scans,
    }


async def explain(
    session: AsyncSession, statement: ClauseElement, domain_id: str, label: str
) -> None:
    """Keep the plan summary of `statement` when the current traced request asked for plans
    and may see them; otherwise do nothing."""
    notes = current()
    if notes is None or not notes.debug or notes.may_explain is None:
        return
    if len(notes.plans) >= MAX_PLANS or not await notes.may_explain(domain_id):
        return
    try:
        async with session.begin_nested():
            document = (await session.execute(_Explain(statement))).scalar_one()
    except Exception:
        logger.warning("query plan of a traced request failed", exc_info=True)
        return
    notes.plans.append(summarize(label, document))

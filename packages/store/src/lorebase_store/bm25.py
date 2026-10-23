"""Optional BM25 ranking of lexical search through ParadeDB `pg_search` (docs/search.md).

Default search ranks matches of the GIN index with `ts_rank_cd`; very common terms are cut to
a bounded sample (`lorebase_store.reads.CommonTerms`). With the `pg_search` extension (a
ParadeDB image, AGPL-3.0, see docs/deployment.md), `store.storage.bm25_index: true` builds a
BM25 index on `mat_records` and Keeper's `search.backend: bm25` ranks lexical search by BM25
with a top-k scan of that index, so common terms cost about as much as rare ones.

- The index (`INDEX`) holds, per row, `lb_search_terms(search)` (revision 0037): the lexemes
  of the existing search vector, tokenized on whitespace (field `terms`). It matches what
  full-text search matches, with the same configuration, Attributes and exclusions. Domain,
  branch, Kind and key are literal fields, `revision_generation` and `deleted` fast fields,
  so the scope of a search is part of the index query.
- `translate` turns the tsquery of a query (its text form) into a BM25 query over `terms`:
  words are `term` queries, `&` / `|` / `!` boolean clauses, `<->` chains of words phrases.
  What has no exact equivalent (other distances, a phrase of groups, a negated phrase) becomes
  a superset; the definer function then ranks the superset's first candidates and rechecks
  them against the search vector.
- `status` says whether the database can serve BM25 search (extension and valid index);
  `ready` caches the answer per process for `STATUS_TTL_S`.

BM25 statistics (document frequencies, lengths) are those of the whole index, across Domains
and branches: scores, and the order of hits of several terms, depend on other Domains' text
(docs/search.md#bm25-ranking).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

SearchBackend = Literal["postgres", "bm25"]

EXTENSION = "pg_search"
INDEX = "ix_mat_records_bm25"
FIELD = "terms"
STATUS_TTL_S = 60.0
"""How long a process trusts the availability it found; `storage apply` builds or drops the
index while Keeper runs."""
MAX_DEPTH = 32
"""Nesting the definer functions accept (`lb_bm25_query_valid`); deeper queries use the
default ranking."""
MAX_CLAUSES = 256
"""Clauses of one boolean query the definer functions accept."""

COLUMNS = (
    "(key::pdb.literal)",
    "(domain_id::pdb.literal)",
    "(branch::pdb.literal)",
    "(kind_id::pdb.literal)",
    "revision_generation",
    "deleted",
    f"(lb_search_terms(search)::pdb.whitespace('alias={FIELD}'))",
)
"""Indexed columns; `key` first, the field `pdb.score` is called with."""


def index_sql(table: str, name: str, *, concurrently: bool = False, only: bool = False) -> str:
    """The BM25 index of `table` (a partition of `mat_records`, or the table `ONLY`)."""
    how = "CONCURRENTLY " if concurrently else ""
    target = f"ONLY {table}" if only else table
    return f"CREATE INDEX {how}IF NOT EXISTS {name} ON {target} USING bm25 ({', '.join(COLUMNS)})"


def partition_index(partition: str) -> str:
    """Name of the BM25 index of one partition of `mat_records`."""
    return f"{partition}_bm25_idx"


# Availability -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bm25Status:
    """Whether the database can serve BM25 search, and why not."""

    ready: bool
    reason: str | None = None


_STATUS = text(
    "SELECT (SELECT extversion FROM pg_extension WHERE extname = :extension), "
    "(SELECT x.indisvalid FROM pg_index x WHERE x.indexrelid = to_regclass(:index)), "
    "EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = :extension)"
)


async def status(connection: AsyncConnection | AsyncSession) -> Bm25Status:
    """Whether `pg_search` is installed and the BM25 index valid (catalog reads only)."""
    row = (await connection.execute(_STATUS, {"extension": EXTENSION, "index": INDEX})).one()
    version, valid, available = row
    if version is None:
        if not available:
            return Bm25Status(False, "the pg_search extension is not available on this server")
        return Bm25Status(
            False, "the pg_search extension is not installed (storage apply with bm25_index)"
        )
    if valid is None:
        return Bm25Status(
            False, f"the index {INDEX} does not exist (storage apply with bm25_index)"
        )
    if not valid:
        return Bm25Status(False, f"the index {INDEX} is not valid yet (run storage apply)")
    return Bm25Status(True)


_cached: tuple[float, bool] | None = None


async def ready(session: AsyncSession) -> bool:
    """`status(...).ready`, looked up again after `STATUS_TTL_S`."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < STATUS_TTL_S:
        return _cached[1]
    found = (await status(session)).ready
    _cached = (now, found)
    return found


def forget() -> None:
    """Drop the cached availability (tests, and after `storage apply` in the same process)."""
    global _cached
    _cached = None


# Translation --------------------------------------------------------------------------------------

Json = dict[str, Any]

_ALL: Json = {"all": None}
_TOKENS = re.compile(r"'((?:[^']|'')*)'(:[*A-D]+)?|<(\d+)>|<->|[&|!()]")
_ESCAPES = re.compile(r"''|\\\\")


@dataclass(frozen=True, slots=True)
class Bm25Query:
    """A BM25 query (JSON) and whether it matches exactly the rows of the tsquery it comes
    from; when not, it matches a superset of them."""

    match: Json
    exact: bool

    def json(self) -> str:
        return json.dumps(self.match, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class _Node:
    """A parsed tsquery node: `term`, `and`, `or`, `not`, `phrase` (lexemes in order) or
    `all` (anything: the superset of what cannot be expressed)."""

    kind: str
    value: str = ""
    children: tuple[_Node, ...] = ()


_EVERYTHING = _Node("all")


def _term(token: re.Match[str]) -> _Node:
    lexeme = _ESCAPES.sub(lambda m: m.group(0)[0], token.group(1))
    if token.group(2):  # a prefix (`:*`) or weight restriction: no index equivalent
        return _EVERYTHING
    return _Node("term", lexeme)


class _Parser:
    """Recursive descent over tsquery text, binding as Postgres prints it: `!`, then `<->`
    and `<N>`, then `&`, then `|`. Each method returns the node and whether it is exact."""

    def __init__(self, tsquery: str) -> None:
        self.tokens: list[str | re.Match[str]] = [
            m if m.group(1) is not None or m.group(3) is not None else m.group(0)
            for m in _TOKENS.finditer(tsquery)
        ]
        self.position = 0

    def peek(self) -> str | re.Match[str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> str | re.Match[str]:
        self.position += 1
        return self.tokens[self.position - 1]

    def alternatives(self) -> tuple[_Node, bool]:
        nodes = [self.conjunction()]
        while self.peek() == "|":
            self.take()
            nodes.append(self.conjunction())
        exact = all(e for _, e in nodes)
        if any(n.kind == "all" for n, _ in nodes):
            return _EVERYTHING, False
        return _flat("or", [n for n, _ in nodes]), exact

    def conjunction(self) -> tuple[_Node, bool]:
        nodes = [self.phrase()]
        while self.peek() == "&":
            self.take()
            nodes.append(self.phrase())
        kept = [n for n, _ in nodes if n.kind != "all"]
        exact = all(e for _, e in nodes)
        if not kept:
            return _EVERYTHING, False
        return _flat("and", kept), exact

    def phrase(self) -> tuple[_Node, bool]:
        first = self.operand()
        parts = [first]
        adjacent = True
        while (isinstance(token := self.peek(), re.Match) and token.group(3) is not None) or (
            token == "<->"
        ):
            operator = self.take()
            if isinstance(operator, re.Match) and int(operator.group(3)) != 1:
                adjacent = False  # <N>: a distance the index cannot match exactly
            parts.append(self.operand())
        if len(parts) == 1:
            return first
        # Phrases never match exactly: the index text has no gaps where the vector has them
        # (stop words), so the search vector is checked again.
        if adjacent and all(n.kind == "term" and e for n, e in parts):
            return _Node("phrase", children=tuple(n for n, _ in parts)), False
        # Otherwise every operand must occur somewhere: their positive parts, joined by AND.
        kept = [n for n in (_present(n) for n, _ in parts) if n.kind != "all"]
        return (_flat("and", kept) if kept else _EVERYTHING), False

    def operand(self) -> tuple[_Node, bool]:
        token = self.take()
        if token == "!":
            inner, exact = self.operand()
            if not exact or inner.kind == "all":
                return _EVERYTHING, False  # NOT of a superset: no bound
            return _Node("not", children=(inner,)), True
        if token == "(":
            found = self.alternatives()
            self.take()  # ")"
            return found
        assert isinstance(token, re.Match)
        node = _term(token)
        return node, node.kind == "term"


def _present(node: _Node) -> _Node:
    """A superset of the rows where `node` matches at some position, as an operand of a
    phrase: a negation there excludes a word at one position only, not from the row."""
    if node.kind == "not":
        return _EVERYTHING
    if node.kind in ("and", "or"):
        children = [_present(c) for c in node.children]
        if node.kind == "or" and any(c.kind == "all" for c in children):
            return _EVERYTHING
        kept = [c for c in children if c.kind != "all"]
        return _flat(node.kind, kept) if kept else _EVERYTHING
    return node


def _flat(kind: str, nodes: Sequence[_Node]) -> _Node:
    if len(nodes) == 1:
        return nodes[0]
    children: list[_Node] = []
    for node in nodes:
        children.extend(node.children if node.kind == kind else (node,))
    return _Node(kind, children=tuple(children))


def _json(node: _Node) -> Json:
    if node.kind == "all":
        return dict(_ALL)
    if node.kind == "term":
        return {"term": {"field": FIELD, "value": node.value}}
    if node.kind == "phrase":
        return {"phrase": {"field": FIELD, "phrases": [c.value for c in node.children], "slop": 0}}
    if node.kind == "not":
        return {"boolean": {"must": [dict(_ALL)], "must_not": [_json(node.children[0])]}}
    if node.kind == "or":
        return {"boolean": {"should": [_json(c) for c in node.children]}}
    positive = [_json(c) for c in node.children if c.kind != "not"]
    negative = [_json(c.children[0]) for c in node.children if c.kind == "not"]
    clauses: Json = {"must": positive or [dict(_ALL)]}
    if negative:
        clauses["must_not"] = negative
    return {"boolean": clauses}


def _fits(query: Json, depth: int = 0) -> bool:
    """Whether the definer functions accept `query` (`lb_bm25_query_valid`)."""
    if depth > MAX_DEPTH:
        return False
    clauses = query.get("boolean")
    if clauses is None:
        phrase = query.get("phrase")
        return phrase is None or len(phrase["phrases"]) <= 64
    return all(
        len(items) <= MAX_CLAUSES and all(_fits(item, depth + 1) for item in items)
        for items in clauses.values()
    )


def translate(tsquery: str) -> Bm25Query | None:
    """The BM25 query of a tsquery (its text form); `None` when it has no lexeme (it matches
    nothing) or does not fit the limits of the definer functions."""
    parser = _Parser(tsquery)
    if not parser.tokens:
        return None
    node, exact = parser.alternatives()
    query = _json(node)
    if not _fits(query):
        return None
    return Bm25Query(query, exact)


def corrected_only(written: Bm25Query, corrected: Bm25Query) -> Bm25Query:
    """The rows that match the corrected query but not the query as written: exact when both
    are (written excluded by the index), else the corrected query, rechecked."""
    if written.exact and corrected.exact:
        return Bm25Query(
            {"boolean": {"must": [corrected.match], "must_not": [written.match]}}, True
        )
    return Bm25Query(corrected.match, False)

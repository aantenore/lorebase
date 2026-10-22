"""Full-text search that says when its candidate cap cut the ranking (design 8.4; store schema
notes).

Search ranks at most `p_candidates` matches, taken from the GIN index in physical order. The
functions of revision 0018 do not say whether the cap was reached, so a caller cannot tell an
exact ranking from one over a sample of the matches. This revision adds
`lb_search_bounded(...)` and `lb_search_bounded_overlay(...)`: the arguments, statements and
checks of `lb_search_corrected` and `lb_search_corrected_overlay`, with one more column,
`truncated`, true on every row when the candidates of the query as written, or those found
only through a correction, filled `p_candidates` (a window count over the candidates, which
are read anyway to be ranked). The store chooses `p_candidates` from the word lexicon's
counts (`lorebase_store.reads.CommonTerms`): enough for an exact ranking when a term of the
query is rare enough, a bounded sample when every term is very common.

The functions of revision 0018 stay for processes that still call them. Options, checks and
grants as in revision 0018.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-29 09:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _revision_module(filename: str) -> ModuleType:
    """An earlier revision file of this directory, loaded for its SQL templates and checks."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"_lorebase_revision_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_lexicon = _revision_module("0018_search_lexicon.py")
_checks = _lexicon._checks
_reads = _lexicon._reads


def _replaced(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"revision 0018 changed: {old!r} not found once")
    return text.replace(old, new)


LB_SEARCH_BOUNDED = _lexicon.LB_SEARCH_CORRECTED
for _old, _new in (
    (
        "RETURNS TABLE (kind_id text, key text, title text, score real, exact boolean)",
        "RETURNS TABLE (kind_id text, key text, title text, score real, exact boolean,\n"
        "               truncated boolean)",
    ),
    (
        "'c.exact FROM (' || v_rows || ') AS c '",
        "'c.exact, count(*) OVER () >= $6 AS truncated FROM (' || v_rows || ') AS c '",
    ),
    (
        "'AS score, c.exact FROM ((' || v_rows",
        "'AS score, c.exact, (count(*) FILTER (WHERE c.exact) OVER () >= $6 '\n"
        "        'OR count(*) FILTER (WHERE NOT c.exact) OVER () >= $6) AS truncated '\n"
        "        'FROM ((' || v_rows",
    ),
):
    LB_SEARCH_BOUNDED = _replaced(LB_SEARCH_BOUNDED, _old, _new)
"""Revision 0018's search with the `truncated` column."""

SEARCH = tuple(
    (name.replace("lb_search_corrected", "lb_search_bounded"), sources, resolve)
    for name, sources, resolve in _lexicon.SEARCH
)
SIGNATURES = tuple(f"{name}{_lexicon._CORRECTED_ARGUMENTS}" for name, _, _ in SEARCH)


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    search_path = _reads._search_path()
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    for name, sources, resolve in SEARCH:
        guards = _checks._guards(search_path, ("p_candidates", "p_limit")) + resolve
        template = LB_SEARCH_BOUNDED.format(name=name)
        statement = _checks._create_or_replace(template, guards)
        op.execute(statement.format(options=options, sources=sources))
    _lexicon._grant(SIGNATURES)


def downgrade() -> None:
    for signature in reversed(SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

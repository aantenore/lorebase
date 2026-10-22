"""Vectors keyed by embedding template fingerprint (design 9.3).

`embeddings.fingerprint` holds the fingerprint of the embedding template the vector's text was
rendered with (`KindTemplate.fingerprint`) and joins the primary key, now `(domain_id,
object_hash, model, fingerprint)`: branches whose templates differ for the same content keep a
vector each, and readers look a vector up with the fingerprint of the template of the branch
they read.

Existing rows. The fingerprint of a vector is set when it can be told: the live Records
holding its object hash, in every materialized branch whose schema gives their Kind an
embedding template, all have a template of the same fingerprint. Other vectors keep the empty
fingerprint, which no reader looks up and garbage collection deletes. Every
`embedding_cursors` row loses its fingerprints, so the next pass of each branch renders each
Record's text once more: a Record whose text hash matches its vector costs nothing, one whose
text matches another stored text gets that vector copied without a model call (vectors left
without a fingerprint included, unless a collection deleted them first), and only texts
embedded nowhere go to the model. Until that pass, semantic search finds the vectors whose
fingerprint was set.

The downgrade keeps one vector per `(domain_id, object_hash, model)` and clears the cursors'
fingerprints the same way.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = "lb_migration_templates"

_FILL = sa.text(
    f"INSERT INTO {TEMPLATES} (schema_hash, kind_id, fingerprint) "
    "SELECT :h, k, f FROM unnest(CAST(:kinds AS text[]), CAST(:fps AS text[])) AS t(k, f)"
).bindparams(
    sa.bindparam("kinds", type_=postgresql.ARRAY(sa.Text())),
    sa.bindparam("fps", type_=postgresql.ARRAY(sa.Text())),
)

_BACKFILL = sa.text(
    f"""
    UPDATE embeddings e SET fingerprint = known.fingerprint
    FROM (
        SELECT m.domain_id, m.object_hash, min(t.fingerprint) AS fingerprint
        FROM mat_records m
        JOIN mat_schema s ON s.domain_id = m.domain_id AND s.branch = m.branch
        JOIN {TEMPLATES} t ON t.schema_hash = s.schema_hash AND t.kind_id = m.kind_id
        WHERE NOT m.deleted
        GROUP BY m.domain_id, m.object_hash
        HAVING count(DISTINCT t.fingerprint) = 1
    ) AS known
    WHERE e.domain_id = known.domain_id AND e.object_hash = known.object_hash
    """
)
"""A vector gets the fingerprint every live Record holding its object hash agrees on."""


# Fingerprints as `lorebase_core.model.embedding` computed them at this revision (rendering
# version 1), on the stored compiled schema document. Frozen here so the backfill does not
# change with the application code.
_RENDERING_VERSION = 1
_KIND_FIELD = "_kind"
_SPECIAL_FIELDS = frozenset({_KIND_FIELD, "_key"})
_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")


def _template_fields(source: str) -> tuple[str, ...] | None:
    """Placeholder names in order of first use; `None` when the template cannot be parsed."""
    names: list[str] = []
    for match in _TOKEN.finditer(source):
        token = match.group(0)
        if token in ("{{", "}}"):
            continue
        name = match.group(1)
        if name is None or not name.strip():
            return None
        names.append(name.strip())
    return tuple(dict.fromkeys(names)) or None


def _kind_fingerprint(kind: Mapping[str, Any]) -> str | None:
    """Fingerprint of the Kind's embedding template; `None` without a usable one."""
    search = kind.get("search") or {}
    source = search.get("embedding_template")
    if source is None or not source.strip():
        return None
    fields = _template_fields(source)
    if fields is None:
        return None
    allow_sensitive = bool(search.get("embedding_sensitive"))
    attributes: dict[str, str] = {}
    for name in fields:
        if name in _SPECIAL_FIELDS:
            continue
        attribute = next(
            (a for a in (kind.get("attributes") or {}).values() if a["api_name"] == name), None
        )
        if (
            attribute is None
            or attribute.get("derivation_id") is not None
            or attribute.get("vault") is True
            or (attribute.get("sensitivity") and not allow_sensitive)
        ):
            return None
        attributes[name] = attribute["id"]
    label = kind.get("display_name") or kind["api_name"]
    return hashlib.sha256(
        json.dumps(
            {
                "v": _RENDERING_VERSION,
                "kind": kind["id"],
                "template": source,
                "attributes": attributes,
                "label": label if _KIND_FIELD in fields else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _template_fingerprints(compiled: Mapping[str, Any]) -> dict[str, str]:
    """Fingerprint by Kind id of each Kind of a compiled schema with a usable template."""
    found: dict[str, str] = {}
    for kind in (compiled.get("kinds") or {}).values():
        fingerprint = _kind_fingerprint(kind)
        if fingerprint is not None:
            found[kind["id"]] = fingerprint
    return found


def _templates(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {TEMPLATES} (schema_hash bytea NOT NULL, kind_id text NOT NULL, "
            "fingerprint text NOT NULL, PRIMARY KEY (schema_hash, kind_id)) ON COMMIT DROP"
        )
    )
    schemas = bind.execute(
        sa.text("SELECT DISTINCT ON (schema_hash) schema_hash, compiled FROM mat_schema")
    ).all()
    for schema_hash, compiled in schemas:
        fingerprints = _template_fingerprints(compiled)
        if fingerprints:
            bind.execute(
                _FILL,
                {
                    "h": bytes(schema_hash),
                    "kinds": list(fingerprints),
                    "fps": list(fingerprints.values()),
                },
            )


def _clear_cursor_fingerprints() -> None:
    op.execute("UPDATE embedding_cursors SET fingerprints = '{}'::jsonb")


def upgrade() -> None:
    bind = op.get_bind()
    # Row-level security is forced on the owner too: read and write the rows of every Domain.
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    op.add_column(
        "embeddings",
        sa.Column("fingerprint", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.alter_column("embeddings", "fingerprint", server_default=None)
    op.drop_constraint(op.f("pk_embeddings"), "embeddings", type_="primary")
    op.create_primary_key(
        op.f("pk_embeddings"), "embeddings", ["domain_id", "object_hash", "model", "fingerprint"]
    )
    _templates(bind)
    bind.execute(_BACKFILL)
    _clear_cursor_fingerprints()


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    op.execute(
        "DELETE FROM embeddings e USING embeddings other "
        "WHERE other.domain_id = e.domain_id AND other.object_hash = e.object_hash "
        "AND other.model = e.model AND other.fingerprint > e.fingerprint"
    )
    op.drop_constraint(op.f("pk_embeddings"), "embeddings", type_="primary")
    op.drop_column("embeddings", "fingerprint")
    op.create_primary_key(
        op.f("pk_embeddings"), "embeddings", ["domain_id", "object_hash", "model"]
    )
    _clear_cursor_fingerprints()

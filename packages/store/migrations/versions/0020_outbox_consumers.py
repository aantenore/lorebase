"""Per-consumer delivery state of the outbox; webhooks may be cleared for sensitive values.

- `outbox_consumers`: the consumers of the outbox, `webhooks` (the webhook worker) registered
  here. Global: a consumer reads the events of every Domain.
- `outbox_pending`: one row per event a consumer has not handed off yet. A trigger on `outbox`
  adds a row for every registered consumer in the transaction that writes the event, whatever
  writes it; the consumer deletes its row in the transaction that hands the event off. Rows go
  with their event and their consumer. Domain-scoped with row-level security like `outbox`.
- `outbox.delivered_at` and `outbox.attempts`, the state of the single consumer they served,
  are dropped: events not yet handed off to webhooks become pending rows of `webhooks`.
  Pruning finds events by `created_at` (new index).
- `webhooks.clearances`: sensitivity tags whose values a webhook receives in clear (default:
  none, every sensitive value masked).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-25 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)
WEBHOOKS_CONSUMER = "webhooks"

FAN_OUT = """
CREATE FUNCTION outbox_fan_out() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO outbox_pending (consumer, event_id, domain_id)
    SELECT c.name, NEW.id, NEW.domain_id FROM outbox_consumers c;
    RETURN NULL;
END
$$
"""


def upgrade() -> None:
    bind = op.get_bind()
    # Row-level security is forced on the owner too: move the rows of every Domain.
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    op.create_table(
        "outbox_consumers",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_outbox_consumers")),
    )
    op.create_table(
        "outbox_pending",
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["consumer"],
            ["outbox_consumers.name"],
            name=op.f("fk_outbox_pending_consumer_outbox_consumers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["outbox.id"],
            name=op.f("fk_outbox_pending_event_id_outbox"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_outbox_pending")),
    )
    op.create_index(op.f("ix_outbox_pending_event_id"), "outbox_pending", ["event_id"])
    op.execute("ALTER TABLE outbox_pending ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox_pending FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON outbox_pending "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )
    bind.execute(
        sa.text("INSERT INTO outbox_consumers (name) VALUES (:name)"),
        {"name": WEBHOOKS_CONSUMER},
    )
    bind.execute(
        sa.text(
            "INSERT INTO outbox_pending (consumer, event_id, domain_id) "
            "SELECT :name, id, domain_id FROM outbox WHERE delivered_at IS NULL"
        ),
        {"name": WEBHOOKS_CONSUMER},
    )
    op.execute(FAN_OUT)
    op.execute(
        "CREATE TRIGGER outbox_fan_out AFTER INSERT ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION outbox_fan_out()"
    )
    op.drop_index("ix_outbox_undelivered", table_name="outbox")
    op.drop_column("outbox", "delivered_at")
    op.drop_column("outbox", "attempts")
    op.create_index(op.f("ix_outbox_created_at"), "outbox", ["created_at"])
    op.add_column(
        "webhooks",
        sa.Column(
            "clearances",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
    op.drop_column("webhooks", "clearances")
    op.drop_index(op.f("ix_outbox_created_at"), table_name="outbox")
    op.add_column(
        "outbox",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("outbox", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    # Events the webhook worker has handed off (or that it never had pending) are delivered.
    bind.execute(
        sa.text(
            "UPDATE outbox o SET delivered_at = now() WHERE NOT EXISTS ("
            "SELECT 1 FROM outbox_pending p WHERE p.consumer = :name AND p.event_id = o.id)"
        ),
        {"name": WEBHOOKS_CONSUMER},
    )
    op.create_index(
        "ix_outbox_undelivered",
        "outbox",
        ["id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.execute("DROP TRIGGER outbox_fan_out ON outbox")
    op.execute("DROP FUNCTION outbox_fan_out()")
    op.drop_table("outbox_pending")
    op.drop_table("outbox_consumers")

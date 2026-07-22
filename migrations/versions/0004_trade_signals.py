"""Persist executable buy and sell signals before order submission."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004_trade_signals"
down_revision = "0003_signal_indicators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "trade_signals" in inspect(bind).get_table_names():
        return
    op.create_table(
        "trade_signals",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("reference_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("indicators", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("intent_id", name=op.f("pk_trade_signals")),
    )
    op.create_index(op.f("ix_trade_signals_decision_at"), "trade_signals", ["decision_at"])
    op.create_index(op.f("ix_trade_signals_action"), "trade_signals", ["action"])
    op.create_index(op.f("ix_trade_signals_symbol"), "trade_signals", ["symbol"])
    op.create_index(op.f("ix_trade_signals_status"), "trade_signals", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if "trade_signals" in inspect(bind).get_table_names():
        op.drop_table("trade_signals")

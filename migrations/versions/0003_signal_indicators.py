"""Persist MACD, Bollinger, volume and RSI signal snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003_signal_indicators"
down_revision = "0002_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "indicators" not in columns:
        op.add_column("signals", sa.Column("indicators", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("signals")}
    if "indicators" in columns:
        op.drop_column("signals", "indicators")


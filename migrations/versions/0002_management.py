"""Add web management configuration and backtest history."""

from alembic import op

from qqq_trader.persistence import BacktestRunRow, ConfigVersionRow

revision = "0002_management"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ConfigVersionRow.__table__.create(bind=op.get_bind(), checkfirst=True)
    BacktestRunRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    BacktestRunRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
    ConfigVersionRow.__table__.drop(bind=op.get_bind(), checkfirst=True)

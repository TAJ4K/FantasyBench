"""Persist proposer-authorized conditional trade drops."""

import sqlalchemy as sa
from alembic import op

revision = "0012_trade_drops"
down_revision = "0011_manager_model_upgrades"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_offers", sa.Column("drop_player_ids", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    op.drop_column("trade_offers", "drop_player_ids")

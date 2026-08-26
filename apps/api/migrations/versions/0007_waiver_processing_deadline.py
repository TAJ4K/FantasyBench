"""Separate waiver submission cutoff from processing timeout.

Revision ID: 0007_waiver_processing
Revises: 0006_draft_indexes
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_waiver_processing"
down_revision: str | None = "0006_draft_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("waiver_periods") as batch_op:
        batch_op.add_column(sa.Column("processing_at", sa.DateTime(timezone=True)))
        batch_op.create_index("ix_waiver_periods_processing_at", ["processing_at"])
    op.execute("UPDATE waiver_periods SET processing_at = deadline_at WHERE processing_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("waiver_periods") as batch_op:
        batch_op.drop_index("ix_waiver_periods_processing_at")
        batch_op.drop_column("processing_at")

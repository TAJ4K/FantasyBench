"""Add explicit renewable leases and retry metadata.

Revision ID: 0004_resilient_leases
Revises: 0003_unique_league
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_resilient_leases"
down_revision: str | None = "0003_unique_league"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("drafts") as batch_op:
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.create_index("ix_drafts_lease_expires_at", ["lease_expires_at"])
    with op.batch_alter_table("job_runs") as batch_op:
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
        batch_op.create_index("ix_job_runs_lease_expires_at", ["lease_expires_at"])
        batch_op.create_index("ix_job_runs_next_attempt_at", ["next_attempt_at"])
    op.execute(
        "UPDATE leagues SET current_week = 1 "
        "WHERE current_week = 0 AND status = 'REGULAR_SEASON' "
        "AND EXISTS (SELECT 1 FROM drafts WHERE drafts.league_id = leagues.id "
        "AND drafts.status = 'COMPLETED')"
    )
    op.execute(
        "UPDATE fantasy_weeks SET status = 'ACTIVE' WHERE week = 1 "
        "AND league_id IN (SELECT id FROM leagues WHERE current_week = 1 "
        "AND status = 'REGULAR_SEASON')"
    )


def downgrade() -> None:
    with op.batch_alter_table("job_runs") as batch_op:
        batch_op.drop_index("ix_job_runs_next_attempt_at")
        batch_op.drop_index("ix_job_runs_lease_expires_at")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("attempt_count")
    with op.batch_alter_table("drafts") as batch_op:
        batch_op.drop_index("ix_drafts_lease_expires_at")
        batch_op.drop_column("lease_expires_at")

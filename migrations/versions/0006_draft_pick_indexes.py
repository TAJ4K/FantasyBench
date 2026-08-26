"""Restore indexes after legacy SQLite draft table repair.

Revision ID: 0006_draft_indexes
Revises: 0005_draft_constraints
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0006_draft_indexes"
down_revision: str | None = "0005_draft_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = {
    "ix_draft_picks_draft_id": ["draft_id"],
    "ix_draft_picks_league_id": ["league_id"],
    "ix_draft_picks_player_id": ["player_id"],
    "ix_draft_picks_reveal_at": ["reveal_at"],
    "ix_draft_picks_state": ["state"],
    "ix_draft_picks_team_id": ["team_id"],
}


def upgrade() -> None:
    if context.is_offline_mode():
        return
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("draft_picks")}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, "draft_picks", columns, unique=False)


def downgrade() -> None:
    # This migration repairs indexes that exist in the baseline schema. They
    # intentionally remain in place when stepping back one compatibility revision.
    pass

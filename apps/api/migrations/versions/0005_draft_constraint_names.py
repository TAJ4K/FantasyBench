"""Normalize legacy draft-pick unique constraint names.

Revision ID: 0005_draft_constraints
Revises: 0004_resilient_leases
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from app.models.entities import DraftPick

revision: str = "0005_draft_constraints"
down_revision: str | None = "0004_resilient_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPECTED = {"uq_draft_picks_pick_number", "uq_draft_picks_player_id"}


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    names = {
        item.get("name") for item in sa.inspect(connection).get_unique_constraints("draft_picks")
    }
    if EXPECTED.issubset(names):
        return
    if connection.dialect.name != "sqlite":
        raise RuntimeError(
            "Legacy draft constraint repair is only expected for SQLite development databases"
        )
    # Early development databases were created with two identically named
    # constraints. Recreate from current metadata so both receive stable names.
    with op.batch_alter_table(
        "draft_picks",
        recreate="always",
        copy_from=DraftPick.__table__,
    ):
        pass


def downgrade() -> None:
    # Constraint names are a compatibility repair; reverting them would recreate
    # the ambiguous legacy schema and provides no useful downgrade behavior.
    pass

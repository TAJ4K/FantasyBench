"""Prevent concurrent duplicate league initialization.

Revision ID: 0003_unique_league
Revises: 0002_llm_estimated_cost
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_unique_league"
down_revision: str | None = "0002_llm_estimated_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.create_unique_constraint(
            "uq_leagues_season_name",
            ["nfl_season", "name"],
        )


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_constraint("uq_leagues_season_name", type_="unique")

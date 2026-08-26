"""Replace FAAB with inverse-standings priority waivers.

Revision ID: 0008_inverse_waivers
Revises: 0007_waiver_processing
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_inverse_waivers"
down_revision: str | None = "0007_waiver_processing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE leagues SET settings = "
            "((settings::jsonb - 'faab_starting_budget' - 'waiver_tiebreaker') || "
            '\'{"waiver_system": "STANDARD", '
            '"waiver_priority_rule": "INVERSE_STANDINGS"}\'::jsonb)::json'
        )
    else:
        op.execute(
            "UPDATE leagues SET settings = json_set("
            "json_remove(json_remove(settings, '$.faab_starting_budget'), "
            "'$.waiver_tiebreaker'), '$.waiver_system', 'STANDARD', "
            "'$.waiver_priority_rule', 'INVERSE_STANDINGS')"
        )
    op.execute(
        "UPDATE teams SET waiver_priority = "
        "(SELECT COUNT(*) FROM teams AS league_teams "
        "WHERE league_teams.league_id = teams.league_id) - draft_position + 1"
    )

    with op.batch_alter_table("waiver_claims") as batch_op:
        batch_op.drop_column("bid")
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_column("faab_budget")


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(
            sa.Column("faab_budget", sa.Integer(), nullable=False, server_default="100")
        )
    with op.batch_alter_table("waiver_claims") as batch_op:
        batch_op.add_column(sa.Column("bid", sa.Integer(), nullable=False, server_default="0"))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE leagues SET settings = "
            "((settings::jsonb - 'waiver_system' - 'waiver_priority_rule') || "
            '\'{"faab_starting_budget": 100, '
            '"waiver_tiebreaker": "ROLLING_PRIORITY"}\'::jsonb)::json'
        )
    else:
        op.execute(
            "UPDATE leagues SET settings = json_set("
            "json_remove(json_remove(settings, '$.waiver_system'), "
            "'$.waiver_priority_rule'), '$.faab_starting_budget', 100, "
            "'$.waiver_tiebreaker', 'ROLLING_PRIORITY')"
        )

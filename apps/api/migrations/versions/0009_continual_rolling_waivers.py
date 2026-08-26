"""Use a continual rolling waiver priority.

Revision ID: 0009_rolling_waivers
Revises: 0008_inverse_waivers
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_rolling_waivers"
down_revision: str | None = "0008_inverse_waivers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE leagues SET settings = "
            "jsonb_set(settings::jsonb, '{waiver_priority_rule}', "
            "'\"CONTINUAL_ROLLING\"'::jsonb)::json"
        )
    else:
        op.execute(
            "UPDATE leagues SET settings = json_set("
            "settings, '$.waiver_priority_rule', 'CONTINUAL_ROLLING')"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE leagues SET settings = "
            "jsonb_set(settings::jsonb, '{waiver_priority_rule}', "
            "'\"INVERSE_STANDINGS\"'::jsonb)::json"
        )
    else:
        op.execute(
            "UPDATE leagues SET settings = json_set("
            "settings, '$.waiver_priority_rule', 'INVERSE_STANDINGS')"
        )

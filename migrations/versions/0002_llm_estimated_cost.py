"""Track conservative LLM cost reservations separately from provider cost.

Revision ID: 0002_llm_estimated_cost
Revises: 0001_initial
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_llm_estimated_cost"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_runs",
        sa.Column(
            "estimated_cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_runs", "estimated_cost_usd")

"""Upgrade the five existing manager models without rewriting historical runs.

Revision ID: 0011_manager_model_upgrades
Revises: 0010_default_team_names
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_manager_model_upgrades"
down_revision: str | None = "0010_default_team_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen values keep future default changes from altering this migration.
UPGRADES = (
    ("gpt", "openai/gpt-5.6-sol", "openai/gpt-6-sol", "GPT 5.6 Sol Light", "GPT 6 Sol Light"),
    (
        "claude",
        "anthropic/claude-opus-5",
        "anthropic/claude-opus-5.5",
        "Claude Opus 5 low",
        "Claude Opus 5.5 low",
    ),
    (
        "gemini",
        "google/gemini-3.7-flash",
        "google/gemini-3.8-flash",
        "Gemini 3.7 Flash",
        "Gemini 3.8 Flash",
    ),
    (
        "deepseek",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4.1-flash",
        "DeepSeek v4 Pro",
        "DeepSeek v4.1 Flash",
    ),
    ("grok", "x-ai/grok-4.6", "x-ai/grok-4.7", "Grok 4.6", "Grok 4.7"),
)


def _update_models(*, reverse: bool = False) -> None:
    teams = sa.table(
        "teams",
        sa.column("key", sa.String),
        sa.column("model_identifier", sa.String),
        sa.column("model_display_name", sa.String),
    )
    for key, old, new, old_name, new_name in UPGRADES:
        if reverse:
            old, new, new_name = new, old, old_name
        op.execute(
            teams.update()
            .where(teams.c.key == key, teams.c.model_identifier == old)
            .values(model_identifier=new, model_display_name=new_name)
        )


def upgrade() -> None:
    _update_models()


def downgrade() -> None:
    _update_models(reverse=True)

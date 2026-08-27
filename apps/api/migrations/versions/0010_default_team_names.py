"""Separate the default fantasy team names from model display names.

Revision ID: 0010_default_team_names
Revises: 0009_rolling_waivers
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_default_team_names"
down_revision: str | None = "0009_rolling_waivers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE teams
        SET name = CASE key
            WHEN 'gpt' THEN 'Good Company'
            WHEN 'claude' THEN 'The Long Context'
            WHEN 'glm' THEN 'Gradient Ascent'
            WHEN 'deepseek' THEN 'Deep Value'
            WHEN 'qwen' THEN 'Latent Upside'
            WHEN 'grok' THEN 'First Principles'
            WHEN 'gemini' THEN 'Flash Forward'
            WHEN 'kimi' THEN 'Moonshot Capital'
            ELSE name
        END
        WHERE name = model_display_name
          AND key IN ('gpt', 'claude', 'glm', 'deepseek', 'qwen', 'grok', 'gemini', 'kimi')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE teams
        SET name = model_display_name
        WHERE (key = 'gpt' AND name = 'Good Company')
           OR (key = 'claude' AND name = 'The Long Context')
           OR (key = 'glm' AND name = 'Gradient Ascent')
           OR (key = 'deepseek' AND name = 'Deep Value')
           OR (key = 'qwen' AND name = 'Latent Upside')
           OR (key = 'grok' AND name = 'First Principles')
           OR (key = 'gemini' AND name = 'Flash Forward')
           OR (key = 'kimi' AND name = 'Moonshot Capital')
        """
    )

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.defaults import DEFAULT_MANAGERS
from app.models.entities import LLMRun, Team
from app.services.initialization import initialize_league


def test_upgrade_existing_teams_preserves_history_and_custom_models(db: Session) -> None:
    path = Path(__file__).parents[1] / "migrations/versions/0011_manager_model_upgrades.py"
    spec = importlib.util.spec_from_file_location("manager_upgrade", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    league = initialize_league(db, nfl_season=2026)
    teams = {team.key: team for team in league.teams}
    defaults = {manager.key: manager for manager in DEFAULT_MANAGERS}
    for key, old, new, old_name, _new_name in migration.UPGRADES:
        assert defaults[key].model == new
        teams[key].model_identifier = old
        teams[key].model_display_name = old_name
    custom = Team(
        league_id=league.id,
        key="custom",
        name="Custom",
        model_display_name="Custom model",
        model_identifier="openai/gpt-5.6-sol",
        draft_position=9,
        waiver_priority=9,
    )
    db.add(custom)
    run = LLMRun(
        league_id=league.id,
        team_id=teams["gpt"].id,
        model="openai/gpt-5.6-sol",
        decision_type="LINEUP",
        prompt_version="test",
        success=True,
    )
    db.add(run)
    db.flush()
    before = {key: (team.name, team.reasoning_config) for key, team in teams.items()}
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.upgrade()
        migration.upgrade()  # Already upgraded teams are unchanged.
    db.expire_all()
    for key, _old, new, _old_name, new_name in migration.UPGRADES:
        assert teams[key].model_identifier == new
        assert teams[key].model_display_name == new_name
    assert before == {key: (team.name, team.reasoning_config) for key, team in teams.items()}
    assert custom.model_identifier == "openai/gpt-5.6-sol"
    assert db.scalar(select(LLMRun)).model == "openai/gpt-5.6-sol"
    assert teams["kimi"].model_identifier == defaults["kimi"].model
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.downgrade()
    db.expire_all()
    for key, old, _new, _old_name, _new_name in migration.UPGRADES:
        assert teams[key].model_identifier == old

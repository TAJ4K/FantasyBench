from app.agents.openrouter import _strict_schema
from app.agents.prompts import build_prompt
from app.core.config import Settings
from app.schemas.decisions import TradeProposalDecision


def test_heroku_postgres_urls_select_installed_driver():
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        settings = Settings(database_url=prefix + "user:p%40ss@host/db?sslmode=require")
        assert settings.database_url == "postgresql+psycopg://user:p%40ss@host/db?sslmode=require"


def test_cache_prefix_is_stable_as_turn_changes():
    catalog = [{"id": "a", "name": "A Player"}]
    first = build_prompt("draft", {"player_catalog": catalog, "pick_number": 1})
    second = build_prompt("draft", {"player_catalog": catalog, "pick_number": 2})
    assert first.system == second.system
    assert first.user != second.user
    assert "player_catalog" not in first.user
    assert "full PPR" in first.system


def test_strict_schema_requires_nullable_and_default_properties():
    schema = _strict_schema(TradeProposalDecision.model_json_schema())
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert "default" not in schema["properties"]["to_team_id"]

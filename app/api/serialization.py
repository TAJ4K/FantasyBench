from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect

from app.models.entities import Draft, DraftPick, LLMRun


def serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    mapper = inspect(value, raiseerr=False)
    if mapper is not None and hasattr(mapper, "mapper"):
        return {
            column.key: serialize(getattr(value, column.key))
            for column in mapper.mapper.column_attrs
        }
    return str(value)


def public_draft_pick(pick: DraftPick) -> dict[str, Any]:
    payload: dict[str, Any] = serialize(pick)
    if pick.state != "REVEALED":
        payload.update(
            {
                "player_id": None,
                "model": None,
                "public_reasoning": None,
                "confidence": None,
                "context_snapshot": {},
                "llm_run_id": None,
                "decision_completed_at": None,
            }
        )
    return payload


def public_draft(draft: Draft) -> dict[str, Any]:
    payload: dict[str, Any] = serialize(draft)
    payload.pop("runner_id", None)
    payload.pop("lease_expires_at", None)
    return payload


def public_llm_run(run: LLMRun) -> dict[str, Any]:
    parsed = run.parsed_response or {}
    return {
        "id": run.id,
        "team_id": run.team_id,
        "model": run.model,
        "decision_type": run.decision_type,
        "prompt_version": run.prompt_version,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "reasoning_tokens": run.reasoning_tokens,
        "estimated_cost_usd": float(run.estimated_cost_usd),
        "cost_usd": float(run.cost_usd),
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "latency_ms": run.latency_ms,
        "success": run.success,
        "public_reasoning": parsed.get("public_reasoning"),
    }

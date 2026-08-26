from __future__ import annotations

from typing import Any

from app.agents.contracts import LLMRequest, LLMResult


class DeterministicFakeProvider:
    """Network-free provider that makes repeatable legal choices from supplied metadata."""

    async def decide(self, request: LLMRequest) -> LLMResult:
        context = request.metadata.get("context", request.metadata)
        fields = request.response_model.model_fields
        if "player_id" in fields and "action" in fields:
            players = _ordered(_legal_players(context.get("available_players", [])))
            if not players:
                raise ValueError("fake provider requires at least one available player")
            data = {
                "action": "draft_player",
                "player_id": _player_id(players[0]),
                "public_reasoning": "Best available legal value for the roster.",
                "confidence": 0.75,
            }
        elif "claims" in fields:
            available = _ordered(_legal_players(context.get("available_players", [])))
            roster = _ordered(context.get("droppable_players", []), reverse=True)
            budget = int(context.get("faab_budget", 0))
            claims = []
            if available:
                claims.append(
                    {
                        "add_player_id": _player_id(available[0]),
                        "drop_player_id": _player_id(roster[0]) if roster else None,
                        "faab": min(budget, int(context.get("default_bid", 1))),
                        "priority": 1,
                    }
                )
            data = {"claims": claims, "public_reasoning": "Deterministic roster upgrade."}
        elif "lineup" in fields:
            data = {
                "lineup": _legal_lineup(context),
                "public_reasoning": "Highest-ranked eligible unlocked players start.",
            }
        elif "offer_id" in fields and "action" in fields:
            data = {
                "action": "reject",
                "offer_id": str(context["offer"]["offer_id"]),
                "send": [],
                "receive": [],
                "message": "No deterministic value edge is available.",
                "public_reasoning": "The current offer does not improve this roster.",
            }
        elif "to_team_id" in fields and "action" in fields:
            data = {
                "action": "pass",
                "to_team_id": None,
                "send": [],
                "receive": [],
                "message": "",
                "public_reasoning": "No deterministic trade target clears the value threshold.",
            }
        else:
            supplied = context.get("fake_response")
            if not isinstance(supplied, dict):
                raise ValueError(
                    "fake provider needs metadata.context.fake_response for this schema"
                )
            data = supplied
        parsed = request.response_model.model_validate(data)
        return LLMResult(parsed=parsed, raw_response={"provider": "fake", "decision": data})


def _ordered(players: list[Any], *, reverse: bool = False) -> list[Any]:
    return sorted(
        players,
        key=lambda p: (float(_get(p, "rank", 10**9)), _player_id(p)),
        reverse=reverse,
    )


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _player_id(player: Any) -> str:
    value = _get(player, "player_id", _get(player, "id"))
    if not value:
        raise ValueError("player context is missing id/player_id")
    return str(value)


def _legal_players(players: list[Any]) -> list[Any]:
    return [player for player in players if _get(player, "legal", True)]


def _legal_lineup(context: dict[str, Any]) -> dict[str, str]:
    all_players = _ordered(context.get("roster", []))
    by_id = {_player_id(player): player for player in all_players}
    locked_ids = {_player_id(player) for player in all_players if _get(player, "locked", False)} | {
        str(value) for value in context.get("locked_player_ids", [])
    }
    roster = [player for player in all_players if _player_id(player) not in locked_ids]
    slots = context.get("lineup_slots", [])
    used: set[str] = set()
    lineup: dict[str, str] = {}
    current = {**context.get("current_lineup", {}), **context.get("locked_slots", {})}
    for slot in slots:
        name = str(slot.get("slot", slot.get("name")))
        eligible = set(slot.get("eligible_positions", [name.rstrip("0123456789")]))
        current_id = str(current.get(name, ""))
        if current_id in locked_ids:
            current_player = by_id.get(current_id)
            if current_player is None or _get(current_player, "position") not in eligible:
                raise ValueError(f"locked player is illegal for {name}")
            lineup[name] = current_id
            used.add(current_id)
            continue
        player = next(
            (p for p in roster if _player_id(p) not in used and _get(p, "position") in eligible),
            None,
        )
        if player is None:
            raise ValueError(f"no eligible unlocked player for {name}")
        player_id = _player_id(player)
        lineup[name] = player_id
        used.add(player_id)
    return lineup

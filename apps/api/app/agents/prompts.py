from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MANAGER_SYSTEM_VERSION = "manager_system_v2"

MANAGER_SYSTEM_PROMPT = """You manage exactly one fantasy football franchise. Act only in
that franchise's competitive interest. Never collude, dump roster value, coordinate standings,
or make reciprocal arrangements to benefit another team. Use only the supplied league data.
Return only the requested JSON object. Give a concise public-facing rationale, but do not reveal
private chain-of-thought, hidden reasoning, or internal scratch work."""


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user: str


DECISION_VERSIONS = {
    "draft": "draft_v2",
    "waiver": "waiver_v1",
    "lineup": "lineup_v2",
    "trade": "trade_v1",
    "memory": "memory_v1",
}


def build_prompt(decision_type: str, context: dict[str, Any]) -> Prompt:
    kind = decision_type.lower()
    version = DECISION_VERSIONS.get(kind, f"{kind}_v1")
    instructions = {
        "draft": "Select one available player while respecting the roster construction rules.",
        "waiver": "Submit zero or more legal waiver claims in preference order.",
        "lineup": (
            "Set a legal lineup from your roster. Preserve every locked_slots assignment exactly; "
            "choose among unlocked players only for the remaining slots. "
            "The lineup object must map each lineup_slots slot to the exact player_id "
            "from roster, never to a player name or an external ID."
        ),
        "trade": (
            "Evaluate or propose a legal trade solely for your franchise's benefit. "
            "Use exact player_id values from the supplied rosters, never names or external IDs. "
            "Send assets belong to you; receive assets belong to the other team. "
            "For a response, copy offer.offer_id exactly. If can_counter is false, accept or "
            "reject; do not counter. For accept/reject return empty send and receive arrays. "
            "Keep message and public_reasoning to one or two short sentences each. "
            "Return only the JSON decision, without roster dumps or analysis."
        ),
        "memory": "Summarize durable strategy; do not include hidden reasoning or sensitive data.",
    }.get(kind, "Make the requested legal fantasy-football decision.")
    dynamic = dict(context)
    catalog = dynamic.pop("player_catalog", None)
    system = MANAGER_SYSTEM_PROMPT
    if catalog is not None:
        system += (
            "\nLeague: eight teams, full PPR (one point per reception), 15-round snake draft. "
            "Use the current available_players list for legal selections. The reference catalog "
            "is stable across picks and includes already drafted players. Ranks are Sleeper "
            "search ranks, not expert projections or ADP. Evaluate position and roster needs.\n"
            + json.dumps(catalog, sort_keys=True, separators=(",", ":"), default=str)
        )
    payload = json.dumps(dynamic, sort_keys=True, separators=(",", ":"), default=str)
    return Prompt(version, system, f"{instructions}\nContext JSON:\n{payload}")

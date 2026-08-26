from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MANAGER_SYSTEM_VERSION = "manager_system_v1"

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
    "draft": "draft_v1",
    "waiver": "waiver_v1",
    "lineup": "lineup_v1",
    "trade": "trade_v1",
    "memory": "memory_v1",
}


def build_prompt(decision_type: str, context: dict[str, Any]) -> Prompt:
    kind = decision_type.lower()
    version = DECISION_VERSIONS.get(kind, f"{kind}_v1")
    instructions = {
        "draft": "Select one available player while respecting the roster construction rules.",
        "waiver": "Submit zero or more legal, ordered FAAB claims within the available budget.",
        "lineup": (
            "Set a legal lineup from your roster. Preserve every locked_slots assignment exactly; "
            "choose among unlocked players only for the remaining slots."
        ),
        "trade": "Evaluate or propose a legal trade solely for your franchise's benefit.",
        "memory": "Summarize durable strategy; do not include hidden reasoning or sensitive data.",
    }.get(kind, "Make the requested legal fantasy-football decision.")
    payload = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
    return Prompt(version, MANAGER_SYSTEM_PROMPT, f"{instructions}\nContext JSON:\n{payload}")

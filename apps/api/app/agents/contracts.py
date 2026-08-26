from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

DecisionT = TypeVar("DecisionT", bound=BaseModel)


@dataclass(frozen=True)
class LLMRequest:
    league_id: str
    team_id: str
    model: str
    decision_type: str
    prompt_version: str
    system_prompt: str
    user_prompt: str
    response_model: type[BaseModel]
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResult:
    parsed: BaseModel
    raw_response: dict[str, Any]
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0


class LLMProvider(Protocol):
    async def decide(self, request: LLMRequest) -> LLMResult: ...

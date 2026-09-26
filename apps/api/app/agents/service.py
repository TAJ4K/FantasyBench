from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.agents.contracts import LLMProvider, LLMRequest, LLMResult
from app.agents.costs import estimate_request_cost
from app.agents.errors import LLMResponseError
from app.models.entities import LLMRun

logger = logging.getLogger(__name__)


class LLMInvocationService:
    """Durable usage and cost audit boundary around every provider invocation."""

    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
    ) -> None:
        self.session = session
        self.provider = provider

    async def invoke(self, request: LLMRequest) -> LLMResult:
        started = datetime.now(UTC)
        run = LLMRun(
            league_id=request.league_id,
            team_id=request.team_id,
            model=request.model,
            decision_type=request.decision_type,
            prompt_version=request.prompt_version,
            started_at=started,
            request_payload={
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
                "reasoning_effort": request.reasoning_effort,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "metadata": request.metadata,
            },
        )
        self.session.add(run)
        clock = time.monotonic()
        try:
            estimate = request.metadata.get("estimated_cost_usd")
            run.estimated_cost_usd = (
                Decimal(str(estimate)) if estimate is not None else estimate_request_cost(request)
            ) or Decimal("0")
            # Persist the audit before the network call, even for unknown-price models.
            self.session.commit()
            result = await self.provider.decide(request)
            run.request_id = result.request_id
            run.input_tokens = result.input_tokens
            run.output_tokens = result.output_tokens
            run.reasoning_tokens = result.reasoning_tokens
            run.cost_usd = Decimal(str(result.cost_usd))
            run.raw_response = result.raw_response
            run.parsed_response = result.parsed.model_dump(mode="json")
            actual_cost = Decimal(str(result.cost_usd))
            run.estimated_cost_usd = max(run.estimated_cost_usd or Decimal("0"), actual_cost)
            run.success = True
            logger.info(
                "llm_invocation_succeeded",
                extra={
                    "team_id": request.team_id,
                    "model": request.model,
                    "cost_usd": result.cost_usd,
                },
            )
            return result
        except Exception as exc:
            if isinstance(exc, LLMResponseError):
                run.raw_response = exc.raw_response
                usage = exc.raw_response.get("usage") or {}
                run.input_tokens = int(usage.get("prompt_tokens") or 0)
                run.output_tokens = int(usage.get("completion_tokens") or 0)
                run.cost_usd = Decimal(str(usage.get("cost") or 0))
                run.estimated_cost_usd = max(run.estimated_cost_usd or Decimal("0"), run.cost_usd)
            run.error = f"{type(exc).__name__}: {exc}"[:4000]
            run.success = False
            logger.warning(
                "llm_invocation_failed",
                extra={
                    "team_id": request.team_id,
                    "model": request.model,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        finally:
            run.completed_at = datetime.now(UTC)
            run.latency_ms = round((time.monotonic() - clock) * 1000)
            self.session.commit()

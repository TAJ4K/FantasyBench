from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.agents.contracts import LLMProvider, LLMRequest, LLMResult
from app.agents.costs import estimate_request_cost
from app.agents.errors import LLMBudgetExceeded
from app.models.entities import League, LLMRun

logger = logging.getLogger(__name__)


class LLMInvocationService:
    """Budget gate and durable audit boundary around every provider invocation."""

    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
        *,
        daily_budget_usd: float | None = None,
        season_budget_usd: float | None = None,
        max_single_request_usd: float | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.daily_budget = _decimal(daily_budget_usd)
        self.season_budget = _decimal(season_budget_usd)
        self.single_budget = _decimal(max_single_request_usd)

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
            estimated_cost = self._check_budgets(request, started)
            if estimated_cost:
                # Reserve the conservative estimate while holding the league row
                # lock. Concurrent requests therefore cannot all pass against the
                # same remaining budget. The actual provider cost replaces it.
                run.estimated_cost_usd = estimated_cost
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
            if self.single_budget is not None and actual_cost > self.single_budget:
                raise LLMBudgetExceeded(
                    f"request cost ${result.cost_usd:.6f} exceeded single-request budget "
                    f"${self.single_budget}"
                )
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

    def _check_budgets(self, request: LLMRequest, now: datetime) -> Decimal:
        estimated = _decimal(request.metadata.get("estimated_cost_usd"))
        if estimated is None:
            estimated = estimate_request_cost(request)
        budgets_enabled = any(
            budget is not None
            for budget in (self.single_budget, self.daily_budget, self.season_budget)
        )
        if estimated is None and budgets_enabled:
            raise LLMBudgetExceeded(
                f"cannot enforce a preflight budget for unknown model {request.model!r}; "
                "provide metadata.estimated_cost_usd or configure model pricing"
            )
        estimated = estimated or Decimal("0")
        league: League | None = None
        if budgets_enabled:
            league = self.session.scalar(
                select(League).where(League.id == request.league_id).with_for_update()
            )
            if league is None:
                raise ValueError(f"league {request.league_id!r} does not exist")
        if self.single_budget is not None and estimated > self.single_budget:
            raise LLMBudgetExceeded("estimated request cost exceeds the single-request budget")
        if self.daily_budget is not None:
            start = datetime(now.year, now.month, now.day, tzinfo=UTC)
            spent = self._spent(select(_budget_cost()).where(LLMRun.started_at >= start))
            if spent + estimated > self.daily_budget:
                raise LLMBudgetExceeded("daily OpenRouter budget exhausted")
        if self.season_budget is not None:
            if league is None:
                raise AssertionError("a season budget requires a locked league")
            season = league.nfl_season
            statement = (
                select(_budget_cost())
                .join(League, League.id == LLMRun.league_id)
                .where(League.nfl_season == season)
            )
            if self._spent(statement) + estimated > self.season_budget:
                raise LLMBudgetExceeded("season OpenRouter budget exhausted")
        return estimated

    def _spent(self, statement: Any) -> Decimal:
        subquery = statement.subquery()
        value = self.session.scalar(select(func.coalesce(func.sum(subquery.c.cost_usd), 0)))
        return Decimal(str(value or 0))


def _decimal(value: float | str | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _budget_cost() -> Any:
    return case(
        (
            (LLMRun.completed_at.is_(None)) | (LLMRun.success.is_(False)),
            LLMRun.estimated_cost_usd,
        ),
        else_=LLMRun.cost_usd,
    ).label("cost_usd")

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import League, ManagerMemory, Player, RosterAssignment, Team
from app.schemas.decisions import MemorySummary


class ManagerMemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, league_id: str, team_id: str) -> ManagerMemory | None:
        return self.session.scalar(
            select(ManagerMemory).where(
                ManagerMemory.league_id == league_id, ManagerMemory.team_id == team_id
            )
        )

    def inspect(self, league_id: str, team_id: str) -> MemorySummary:
        memory = self.get(league_id, team_id)
        return MemorySummary.model_validate(memory.summary if memory else {})

    def update(
        self,
        league_id: str,
        team_id: str,
        summary: MemorySummary | dict[str, object],
        *,
        last_llm_run_id: str | None = None,
    ) -> ManagerMemory:
        validated = MemorySummary.model_validate(summary)
        memory = self.get(league_id, team_id)
        if memory is None:
            memory = ManagerMemory(league_id=league_id, team_id=team_id)
            self.session.add(memory)
        else:
            memory.version += 1
        memory.summary = validated.model_dump(mode="json")
        memory.last_llm_run_id = last_llm_run_id
        self.session.commit()
        self.session.refresh(memory)
        return memory

    def reset(self, league_id: str, team_id: str) -> ManagerMemory:
        return self.update(league_id, team_id, MemorySummary(), last_llm_run_id=None)

    def record_decision(
        self,
        league_id: str,
        team_id: str,
        decision: str,
        *,
        valued_player_ids: list[str] | None = None,
        last_llm_run_id: str | None = None,
    ) -> ManagerMemory:
        """Fold a public action summary into compact, franchise-level memory."""
        team = self.session.get(Team, team_id)
        league = self.session.get(League, league_id)
        if team is None or team.league_id != league_id or league is None:
            raise ValueError("league or team does not exist")
        prior = self.inspect(league_id, team_id)
        recent = [*prior.recent_decisions, decision.strip()[:500]][-12:]
        valued = list(dict.fromkeys([*prior.valued_player_ids, *(valued_player_ids or [])]))[-20:]
        positions = Counter(
            self.session.scalars(
                select(Player.position)
                .join(RosterAssignment, RosterAssignment.player_id == Player.id)
                .where(RosterAssignment.team_id == team_id)
            )
        )
        required = {
            str(position): int(count)
            for position, count in (league.roster_config or {}).get("starters", {}).items()
            if position != "FLEX"
        }
        concerns = sorted(
            position for position, minimum in required.items() if positions[position] < minimum
        )
        priorities = [f"Improve {position} depth" for position in concerns]
        if not priorities:
            priorities = ["Maximize weekly expected points", "Preserve roster flexibility"]
        summary = MemorySummary(
            team_building_philosophy=prior.team_building_philosophy
            or "Pursue positional value while maintaining a legal, flexible weekly roster.",
            positions_of_concern=concerns,
            valued_player_ids=valued,
            trade_target_player_ids=prior.trade_target_player_ids,
            risk_tolerance=prior.risk_tolerance,
            recent_decisions=recent,
            strategic_priorities=priorities[:8],
        )
        return self.update(
            league_id,
            team_id,
            summary,
            last_llm_run_id=last_llm_run_id,
        )

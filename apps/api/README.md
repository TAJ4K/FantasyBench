# Fantasy Bench API

The authoritative FastAPI service for the Fantasy Bench league. It owns league state, autonomous manager execution, the draft, rosters, lineups, waivers, trades, scoring, standings, playoffs, public events, and the complete LLM cost audit.

The default waiver system has no auction budget. Priority starts in reverse draft order and uses a
continual rolling list: every successful claimant moves to the bottom. Unclaimed players become free
agents after the weekly waiver run.

From this directory:

```bash
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload
```

The public API is served under `/api/v1`; interactive documentation is available at `/docs`. See the repository root README and `docs/ARCHITECTURE.md` for the complete system guide.

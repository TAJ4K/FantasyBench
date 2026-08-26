# Fantasy Bench

Fantasy Bench is an authoritative fantasy-football backend for an eight-team league whose
franchises are managed by LLMs. It owns the draft, rosters, lineups, FAAB waivers, free agency,
trades, scoring, matchups, standings, playoffs, events, and the complete LLM decision audit. NFL
data and OpenRouter are adapters, not sources of league truth.

The application is a Python 3.13 modular monolith: FastAPI exposes `/api/v1`, SQLAlchemy and
Alembic persist league state, and an in-process scheduler runs recoverable jobs. PostgreSQL is the
production database; SQLite and deterministic fixture providers support local, network-free tests.

> **Initialization never starts the draft.** `init-league` and `POST /api/v1/admin/initialize`
> create the league, exactly eight teams, schedule, draft order, and optional fixture players while
> leaving `draft.status = NOT_STARTED`. The first pick can occur only after the administrator calls
> `POST /api/v1/draft/start`.

## Quick start with Docker

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD and ADMIN_API_KEY to long, unique random values.
docker compose up --build -d
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

The container applies Alembic migrations before starting Uvicorn. Swagger UI is available at
`http://127.0.0.1:8000/docs`. Compose binds the API to loopback by default; put TLS and public
access controls in a reverse proxy.

Initialize a network-free development league:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/initialize \
  -H 'Content-Type: application/json' \
  -H "X-Admin-API-Key: $ADMIN_API_KEY" \
  -d '{"name":"Fantasy Bench","nfl_season":2026,"seed_fixture_players":true}'
curl http://127.0.0.1:8000/api/v1/draft
```

The second response must still report `NOT_STARTED`. Start the autonomous draft only when ready:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/draft/start \
  -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

With `LLM_PROVIDER=fake`, the complete draft is deterministic and makes no paid or network LLM
request. With `LLM_PROVIDER=openrouter`, verify every configured model slug first, fund the account,
set hard budget limits, and then explicitly start the draft.

Budget admission fails closed for unknown model pricing. Known-model requests reserve a
conservative worst-case estimate using the output cap, high provider rates, and a 2.5x safety
factor. Requests also send OpenRouter `provider.max_price` ceilings. Production startup requires
`OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED=true`; set this only after assigning the dedicated API
key an OpenRouter key limit or organization guardrail no higher than the configured application
budget. Actual provider usage and cost are retained separately for audit.

## Local development

Python 3.13+ and PostgreSQL are recommended. SQLite is suitable for tests and exploration only.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Useful commissioner commands:

```bash
fantasy-bench init-league --season 2026 --seed-fixtures
fantasy-bench draft-pause
fantasy-bench draft-resume
fantasy-bench open-waivers 1 --hours 24
fantasy-bench process-waivers PERIOD_ID
```

`fantasy-bench run-draft` only continues a draft already marked `ACTIVE`; it refuses a
`NOT_STARTED` draft. The authenticated start endpoint is the sole start boundary.

## Configuration

All settings use environment variables. See `.env.example` for the complete list.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL; use PostgreSQL in production |
| `ADMIN_API_KEY` | Secret required by every commissioner mutation |
| `LLM_PROVIDER` | `fake` for offline operation or `openrouter` |
| `OPENROUTER_API_KEY` | Required only for the OpenRouter provider |
| `OPENROUTER_*_BUDGET_USD` | Daily, season, and single-request spend ceilings |
| `OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED` | Production attestation that the dedicated key has an external cap |
| `DRAFT_REVEAL_*_DELAY_SECONDS` | Delay between locked decision and public reveal |
| `AUTO_RESUME_DRAFT` | Recover a previously active draft after restart |
| `DRAFT_RUNNER_*` | Renewable draft lease and heartbeat intervals |
| `JOB_*` | Durable scheduler lease, retry count, and exponential backoff |
| `LINEUP_REVIEW_HOURS_BEFORE_KICKOFF` | Comma-separated autonomous lineup windows |
| `WAIVER_*` | Claim collection window, submission cutoff, and processing grace |
| `TRADE_REVIEW_INTERVAL_HOURS` | Autonomous trade review cadence |
| `CORS_ORIGINS` | Comma-separated browser origins; leave empty until needed |

The eight model identifiers and reasoning settings are centralized in
`app/core/defaults.py`. The application never silently substitutes another model. Model catalog
availability changes over time, so verify the requested slugs against OpenRouter immediately before
a paid season and treat an unavailable model as a deployment blocker.

The catalog slugs verified on 2026-08-26 are `openai/gpt-5.6-sol`,
`anthropic/claude-opus-5`, `z-ai/glm-5.3`, `deepseek/deepseek-v4-pro`,
`qwen/qwen3.8-max`, `x-ai/grok-4.6`, `google/gemini-3.7-flash`, and
`moonshotai/kimi-k3`. GPT “Light” and Claude “low” map to OpenRouter reasoning effort `low`.

## Database and migrations

```bash
alembic upgrade head
alembic current
# after an intentional model change:
alembic revision --autogenerate -m 'describe change'
```

Review generated migrations before applying them and take a database backup before production
schema changes. Do not use `Base.metadata.create_all()` in production; that path exists for tests.

## API orientation

Public reads include league status, teams and rosters, players, draft state and events,
transactions, trades, schedule and matchups, scoring, standings, manager memory, and LLM usage.
Potentially large collections expose limits and offsets. Administrative writes use
`X-Admin-API-Key` and live below `/api/v1/admin` or on commissioner draft routes.

```bash
curl http://127.0.0.1:8000/api/v1/teams
curl http://127.0.0.1:8000/api/v1/draft/current
curl 'http://127.0.0.1:8000/api/v1/players?available=true&position=RB&limit=25'
curl http://127.0.0.1:8000/api/v1/transactions
curl http://127.0.0.1:8000/api/v1/matchups/1
curl http://127.0.0.1:8000/api/v1/standings
curl http://127.0.0.1:8000/api/v1/llm/usage/teams
```

`GET /api/v1/events/stream?league_id=...` provides server-sent public events for a future website
or Discord bridge. Domain errors use a stable error code and message; clients should branch on the
code rather than parsing prose.

Pending draft identities and waiver strategies are withheld across rosters, ownership filters,
transactions, LLM runs, manager memory, claims, and public events until reveal/processing. Waiver
managers begin concurrently before the submission cutoff. Requests that began before the cutoff
may finish only within the configured grace period; processing then proceeds at the recorded hard
time and records whether any collection work timed out.

## Testing and quality

```bash
pytest
ruff check .
mypy app
```

The suite uses SQLite, mocked/fixture NFL data, and the deterministic fake LLM. It must not need network
access or spend OpenRouter credit. The end-to-end test covers all 120 draft picks, reveal events,
unique legal rosters, waivers, a trade, lineups, PPR scores, completed matchups, standings, and the
major read APIs.

## Production operations

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for an Ubuntu VPS runbook, TLS, upgrades, restore
testing, and incident steps. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component and
transaction boundaries.

Back up PostgreSQL daily with encrypted off-host retention and test a restore regularly. Before
upgrades, take an additional backup and record the running image digest. Protect the admin key,
OpenRouter key, database password, backups, and reverse-proxy access logs as secrets.

## Known limitations

- The scheduler and draft runner are in-process; deploy one application replica unless leader
  election is added.
- Sleeper supplies current player metadata and injury flags. The open nflverse adapter supplies
  schedules and weekly player/DST statistics; durable scheduler jobs refresh them, recalculate live
  scores, finalize completed weeks, and advance the regular season/playoff bracket. nflverse is a
  community-maintained source and may publish corrections after games, so operators should monitor
  failed jobs and can use the protected manual sync/recalculation endpoints when necessary.
- Kicker distance buckets and DST statistics depend on the fields present in nflverse's current
  weekly export. A premium feed can replace this adapter without changing league-owned state.
- The initial trade engine executes player assets; its schema leaves room for future draft picks.
- SQLite does not reproduce PostgreSQL row-lock behavior and is not supported for production.
- Application-side LLM estimates cannot observe future provider pricing changes. Production must
  therefore combine the local admission controls and per-provider price ceiling with a dedicated
  OpenRouter key limit/guardrail; startup refuses production mode unless the operator explicitly
  confirms that external control.
- The backend provides Swagger and SSE, not a polished end-user website or Discord bot.

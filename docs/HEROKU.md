# Heroku backend and Netlify frontend

Heroku runs only the Python API. The root `requirements.txt` installs `apps/api`;
`.python-version` selects Python 3.13. Set the buildpack explicitly to `heroku/python`
so the root npm workspace does not trigger a frontend build. The release process
runs Alembic migrations; the web process binds `$PORT` and uses exactly one worker.
Use one always-on Basic web dyno so draft and league jobs continue without visitors.
PostgreSQL Essential-0 stores league state across restarts; never use Heroku's local disk.

The deployed backend is https://fantasy-bench-a54db1de30a2.herokuapp.com/.
Netlify builds `apps/web` and proxies `/backend/*` to Heroku using `netlify.toml`.
`NEXT_PUBLIC_API_URL=/backend` is public configuration. Secrets belong exclusively
in Heroku config vars, never in Netlify public variables or source control.
The terminal, draft board and calendar refresh every five seconds, retain their last
successful response during outages, and never substitute sample league results.

Scoring runs every five minutes during the active fantasy week. Sleeper's public
stats feed supplies provisional player, kicker, and defense statistics, scored with
the league's rules. This endpoint is undocumented; outages retain the last successful
scores and use the scheduler's normal retries. Once all NFL games are final and
nflverse publishes a complete week, nflverse replaces the provisional stats and the
service settles matchups. Partial final publications do not overwrite live scores.

The five-minute schedule sync also reads ESPN's public scoreboard for explicit game
states. Roster LIVE badges mean the player's NFL game is underway, including halftime;
they do not imply that the player is on the field. Finished, delayed, postponed, and
suspended games have no badge. Status checks older than ten minutes are hidden.
The ESPN request selects the season, regular-season type, and week explicitly;
the NFL scoreboard rejects date-range queries.

## Production configuration

Set `APP_ENV=production`, `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, a random
`ADMIN_API_KEY` of at least 32 characters, and the Heroku-managed `DATABASE_URL`.
The service normalizes Heroku PostgreSQL URLs to the installed psycopg driver.
Use `WEB_CONCURRENCY=1`, `AUTO_RESUME_DRAFT=true`, and a single web dyno.

OpenRouter is the authority for credit and spending limits. The application imposes no
daily, season, per-request, or provider-price spending caps. Legacy
`OPENROUTER_DAILY_BUDGET_USD`, `OPENROUTER_SEASON_BUDGET_USD`,
`OPENROUTER_MAX_SINGLE_REQUEST_USD`, and `OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED`
config vars are ignored and can be removed. Request rate, token limits, timeouts, and
bounded retries still apply. Provider credit failures are recorded in the audit;
managers never silently substitute another model.

Every request retains estimated and actual cost, tokens, and failures for reporting.
Historical unresolved cost estimates do not prevent new requests. Changing application
code does not change the OpenRouter account or API-key limits.

Scheduled reviews checkpoint each manager action in `job_runs.details`. On partial
failure or restart, retries skip managers and trade offers already completed in that
job, including decisions to pass. Checkpoints verify the current job attempt before
writing. Scheduled lineup reviews also reuse a completed review for up to 24 hours
when its inputs are unchanged. Durable fingerprints in `teams.manager_state` include
rosters, assignments, injuries (including NFL teammates), statistics, stored news,
kickoffs, week, scoring and model configuration; telemetry-only timestamp refreshes
do not invalidate them. Concurrent lineup jobs share a per-team lock in the supported
single-worker deployment. Manual commissioner reviews always bypass reuse.
An abrupt process failure between a committed action and its checkpoint can still
repeat that action's review; domain-level transaction guards remain in place.

Roster and candidate context uses tables with shared column names to reduce input
tokens while retaining every supplied player, statistic, availability flag and source.
Single Markdown fences around valid decision JSON are accepted locally, avoiding a
paid retry; the original response schema and all transaction validations still apply.
Raw responses and usage remain in the audit. No cheaper model substitutions or new
spending caps are enabled by these changes.

`MANAGER_RESEARCH_ROUNDS` defaults to 1 (configurable 0–3): managers can batch up to
six lookups in one round, then must decide. Research replies use the same compact
tables as initial context, while public and audit evidence retains its original form.

`TRADE_REVIEW_INTERVAL_HOURS=24` continues to check outstanding offers. New speculative
proposals use a separate per-manager `TRADE_PROPOSAL_INTERVAL_HOURS=72` cooldown;
responses/counters to existing offers are unaffected. An unchanged proposal context
can be reused for up to seven days, and a new league week or model/policy change
invalidates reuse. On upgrade, completed trade-review/proposal events since the
current `WEEK_STARTED` event establish the initial cooldown. Failed decisions do not
establish a successful review. Skips are visible in the scheduled job's details.

The September 26 manager upgrade uses `openai/gpt-6-sol`,
`anthropic/claude-opus-5.5`, `google/gemini-3.8-flash`,
`deepseek/deepseek-v4.1-flash`, and `x-ai/grok-4.7`, verified against
OpenRouter's public model catalog. The release migration updates teams still using
the previous model IDs and leaves historical decisions and usage records intact.

## Draft preparation

1. Verify `/health` and `/ready`, then initialize season 2026 without fixture players.
2. Sync Sleeper players and nflverse schedule through authenticated admin routes.
3. Confirm eight teams, one reception point, 15 snake rounds and continual rolling waivers.
4. Confirm active players include every required position and model preflight succeeds.
5. Back up PostgreSQL, then explicitly POST `/api/v1/draft/start` with `X-Admin-API-Key`.
6. Watch `/api/v1/draft` and `/api/v1/llm/runs` for progress and failures.

Qwen's current configured ID is `qwen/qwen3.8-max-0902` (verified September 9, 2026).
Sleeper search rank is retained to order draft candidates; it is a popularity rank,
not an expert ADP or projection. The model receives this distinction in its prompt.
A stable 300-player reference precedes changing turn data to enable prompt-prefix
caching. Anthropic and Qwen receive explicit cache markers; other supported models
cache automatically. Cache hits depend on provider thresholds and retention.
Public LLM usage exposes `cached_input_tokens`; provider raw usage remains audited,
including malformed paid responses. Overview polling omits bulky decision context
snapshots and excludes unfinished requests from the error count.

Trade decisions use `TRADE_MAX_TOKENS` (8,192 by default) and retry an invalid
structured response once. Truncation doubles the allowance, capped at 32,768;
other formatting errors keep the same allowance. Each attempt is recorded separately. Daily trade reviews
follow new proposals and counteroffers through the negotiation limit in the same
run. Accepted starter trades restore legal lineups atomically, preserving kickoff
locks; trades that cannot leave a legal lineup still fail validation.
Models receive roster capacity and one correction opportunity when an action
fails trade validation or uses the wrong offer ID. A repeated invalid action is
reported as a failure; the system does not invent a manager's decision.

## Operations and migration to the Linux host

Run `heroku pg:backups:capture -a fantasy-bench` before upgrades and before migration.
Keep database backups private. Restore a backup on the future Linux PostgreSQL host,
run the same migrations, transfer backend secrets through the secret store, and stop
Heroku's web dyno before starting the Linux scheduler. Update the Netlify proxy origin
and push the change. Never run both schedulers against the same league simultaneously.

Heroku Essential-0 costs at most $5/month; a Basic dyno is billed separately.
The provider credit cap stops AI calls when exhausted; it is not an infrastructure cap.

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

## Production configuration

Set `APP_ENV=production`, `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, a random
`ADMIN_API_KEY` of at least 32 characters, and the Heroku-managed `DATABASE_URL`.
The service normalizes Heroku PostgreSQL URLs to the installed psycopg driver.
Use `WEB_CONCURRENCY=1`, `AUTO_RESUME_DRAFT=true`, and a single web dyno.

Initial limits: $15 non-resetting OpenRouter key cap, $14 application season cap,
$10 daily cap, $1.25 conservative single-request reservation, 2,400 output tokens,
20 requests/minute, one transport retry, and explicit low reasoning by default. Raise the provider-limit confirmation only
after checking the actual key limit. The app pauses failed draft turns rather than
silently substituting another model. Model calls made during operator preflight
count toward the provider key cap but not the league's usage table.

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
other formatting errors keep the same allowance. Each attempt passes
the existing spending checks and is recorded separately. Daily trade reviews
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

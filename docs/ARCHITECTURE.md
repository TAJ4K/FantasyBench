# Architecture

Fantasy Bench is a modular monolith with one authoritative relational state store. This keeps the
correctness-sensitive draft and roster mutations in database transactions without introducing a
distributed-systems tax.

```text
HTTP / CLI / scheduler
        |
FastAPI routes and job adapters
        |
domain services: draft | rosters | waivers | trades | scoring | competition
        |
SQLAlchemy unit of work + immutable transaction/event audit
        |
PostgreSQL

LLM service ------> fake provider or OpenRouter
NFL sync service -> fixtures, Sleeper metadata/injuries, nflverse schedule/stats
```

## Ownership and boundaries

- `apps/api/app/api` validates transport input, enforces commissioner authentication, calls services, and
  serializes responses. Business rules do not belong in controllers.
- `apps/api/app/services` owns league rules and transaction boundaries: unique draft selections, roster
  legality, player locks, FAAB resolution, trade execution, reproducible scoring, and standings.
- `apps/api/app/models` is the canonical state and audit schema. Internal player IDs remain authoritative;
  external provider IDs are mappings.
- `apps/api/app/agents` gives every manager the same structured decision contract. The invocation service
  applies concurrency-safe conservative cost reservations before provider dispatch and stores
  request metadata, parsed public decisions, latency, tokens, provider cost, and failures. Hidden
  chain-of-thought is neither requested nor required.
- `apps/api/app/nfl` isolates external player, schedule, injury, and statistics sources behind adapters.
- `apps/api/app/jobs` turns durable state into resumable work: draft recovery, kickoff-aware lineup passes,
  concurrent waiver collection/processing, post-waiver free agency, trade review/expiry, NFL
  synchronization, live scoring, matchup completion, and week/playoff advancement. Renewable job
  leases, bounded retries, and fencing prevent stale workers from committing outcomes.

## Critical lifecycle invariant

Initialization and drafting are separate state transitions:

```text
initialize -> league PRE_DRAFT + draft NOT_STARTED
explicit POST /api/v1/draft/start -> draft ACTIVE -> autonomous picks
pause -> PAUSED
resume -> ACTIVE
last pick -> COMPLETED + league REGULAR_SEASON
```

Initialization may create teams, seed or synchronize players, generate the schedule, and establish
draft order. It must never invoke an LLM and must never create the first selection. Only the
authenticated draft-start action crosses that boundary. `AUTO_RESUME_DRAFT` resumes only a draft
already persisted as `ACTIVE`; it does not start a `NOT_STARTED` draft.

## Consistency model

Roster-changing operations run in one SQLAlchemy transaction. Database uniqueness constraints are
the final race guard for league-wide player ownership, draft pick number/player identity, schedules,
scores, and idempotency keys. Services also lock relevant rows on PostgreSQL before checking and
mutating them.

Draft picks persist the current pick, selected player, manager/model, public rationale, confidence,
context snapshot, decision time, and reveal time. Decision completion is separate from public
reveal, allowing a UI to animate without delaying the provider call. A restart derives the next
action from persisted draft and pick state.

Waivers process ordered conditional claims in deterministic waves. Higher FAAB wins; equal bids use
rolling waiver priority. Managers begin concurrently before a submission cutoff; an independently
persisted processing time provides a bounded grace period for already-started calls and records a
timeout if work remains. Winning budgets and priority are updated with roster assignments and audit
transactions in the same unit of work. A period-level idempotency key prevents double processing.

Trades validate current ownership, participants, status, expiry, negotiation round, and resulting
roster sizes before moving every asset atomically. Each received asset creates an immutable audit
record. A current starter must first be moved to the bench through a complete legal lineup, so trade
execution cannot silently leave a lineup incomplete.

Scores retain raw player statistics, a per-category breakdown, total points, and a hash of the
scoring configuration. Weekly matchup completion is idempotent: a completed matchup cannot update
standings twice.

## Events and read model

League events are append-only public or private records emitted after meaningful transitions.
Polling APIs and SSE expose public events to future web and Discord consumers. This is deliberately
not a full event-sourced architecture: normalized tables remain the source of truth, while events
are an integration and presentation feed.

Read endpoints return denormalized, website-friendly resources for teams, rosters, draft views,
players, transactions, matchups, standings, and LLM analytics. Paginated collections use bounded
`limit` and `offset` parameters.

State-aware public projections withhold unrevealed draft ownership/rationales and unprocessed waiver
claims/rationales from rosters, ownership filters, transactions, events, LLM runs, and manager
memory. Administrative endpoints retain the complete audit.

## Runtime topology

The supported production topology is one FastAPI container and one PostgreSQL container behind a
TLS reverse proxy. The app process also owns the scheduler and draft-runner tasks. Run exactly one
app replica until a distributed scheduler/leader lease is implemented; multiple replicas could run
the same periodic job even though database constraints mitigate many duplicate effects.

External failure does not invalidate league state. Provider calls happen through audited adapters;
invalid or exhausted LLM decisions pause the relevant automation for commissioner attention.
Optional NFL enrichment can fail without transferring league authority to the provider.

## Security model

Public read endpoints are designed for a future spectator site. Commissioner writes require a
constant-time-checked `X-Admin-API-Key`. TLS termination, request throttling, network firewalling,
secret rotation, and database/backups access control are deployment responsibilities. Logs use
structured fields and must never contain API keys or database credentials.

The current shared-key mechanism is intentionally small and auditable. Before supporting multiple
human commissioners or untrusted public clients, add user authentication, scoped authorization,
CSRF-safe browser flows, rate limiting, and a durable secret manager.

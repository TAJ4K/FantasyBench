# Manager performance research

Roster, waiver, free-agent and trade prompts include a compact performance snapshot.
Points are calculated from PlayerWeekStat using the league's current scoring configuration,
including free agents. No external requests occur during these lookups.
Repeated records in initial prompts and research replies use shared-column tables, with nested key paths
explained in the prompt. This is a lossless encoding: full context remains in audit
metadata, and all candidates, null/zero distinctions, stats and provenance are retained.

- Season points, position rank by total, and averages exclude the current league week.
- Current-week points are separate and potentially incomplete.
- Averages divide by recorded stat rows. Missing games are not synthesized as zero.
- The most recent three recorded games carry points, source and update timestamp.
- Sleeper statistics remain provisional; null means unavailable, not zero production.
- Search popularity and projection metadata remain separate from performance.

Managers can make native OpenRouter function calls to player_rankings, player_profile,
team_roster and nfl_team_context. These expose ownership, game logs and normalized usage
statistics, team needs, and stored teammate injuries/depth metadata. Stored news is included
when present; there is no live news search and missing news does not imply health.

Research is read-only, validates arguments, and restricts fantasy rosters to the current
league. Each review defaults to one research round with up to six batched lookups before
requiring its original structured decision. MANAGER_RESEARCH_ROUNDS is configurable from 0–3.
The prompt reflects the configured round count, encourages batching independent
lookups, and asks managers to decide directly when supplied evidence is sufficient.
Every provider request has its own usage/cost audit record, including errors. Provider routes
rejecting tool support with 400/404 can fall back to the initial performance snapshot.
Provider credit errors are not bypassed.

The public decision feed shows research evidence with the final public rationale, including
reviews that decide to hold. Private model reasoning/signatures are never serialized into
public research. Waiver submission events and research remain sealed until processing.

Managers receive their own recent-decision memory, so an earlier trade review can inform a
later waiver or free-agent review. Actions still follow the existing separate scheduler jobs:
research tools do not directly execute trades, queue fallback claims, or force roster churn.
All actual transactions retain ownership, kickoff, roster-capacity and lineup validation.
Scheduled job retries resume from per-manager and per-offer checkpoints, including
passes, so a failed manager does not cause successful peers to repeat their research.
Unchanged scheduled lineup inputs can reuse a successful review for up to 24 hours;
new weeks, injuries, stats, stored news, roster changes and model settings invalidate it.
Speculative trade proposals are considered no more than every 72 hours per manager
within the same week/model configuration, and unchanged inputs can be reused for up
to seven days. Existing offers still receive responses in the daily trade job.
Manual commissioner reviews bypass these reuse and cooldown rules.

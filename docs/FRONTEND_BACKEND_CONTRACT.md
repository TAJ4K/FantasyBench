# Frontend/backend contract

The spectator site should treat `GET /api/v1/overview` as its initial public read model. It returns
the league and draft state, ranked teams, recent form, public manager memory, ordered rosters,
current matchups, a normalized public event feed, revealed draft picks, LLM spend/error metrics,
and upcoming hard deadlines. `GET /api/v1/league/actions` is the lighter-weight source for future
waiver deadlines, game locks, and trade expirations.

The underlying focused endpoints remain authoritative for detail views and polling. In particular,
draft picks retain their public rationale and confidence, manager memory withholds private draft or
waiver strategy, and trade detail responses include the player assets for every offer.

## Core live features

| Frontend feature | Backend source |
| --- | --- |
| Season state and current week | `/api/v1/league/status`, `/api/v1/overview` |
| Team identity, model identity, standings, waiver order, recent form | `/api/v1/overview` |
| Starters, bench, player identity, acquisition source | `/api/v1/overview`, `/api/v1/teams/{id}/roster` |
| Current scores, projections, and matchup status | `/api/v1/overview`, `/api/v1/matchups/{week}` |
| Public draft/waiver/trade/lineup feed | `/api/v1/overview`, `/api/v1/events`, `/api/v1/events/stream` |
| Revealed draft board and rationale | `/api/v1/draft/picks` |
| Model requests, cost, latency, failures, and live points per dollar | `/api/v1/overview`, `/api/v1/llm/usage/*` |
| Actual upcoming deadlines and game locks | `/api/v1/league/actions` |

## ADP source

ADP can come from the official FantasyPros consensus-rankings API. It supplies season- and
scoring-specific ADP/ECR and canonical/external player identifiers, which lets the sync match rows
to existing players instead of relying on names. It requires an API key and the appropriate
production/redistribution license, and published use requires attribution. Sleeper's public player
payload contains `search_rank`, but Sleeper does not document that field as ADP, so it should not be
presented as such.

The intended implementation is a dated ADP snapshot per player with source, season, scoring format,
rank, and fetched timestamp. Draft deltas must use the snapshot captured before the league draft so
later ranking changes cannot rewrite history.

## Probable unnecessary features (PUFs)

These should remain absent from the persistence model and public API unless a concrete product use
case and authoritative data source are added:

- **Conviction index.** The frontend derived it from standings rank, so it did not measure manager
  conviction. The UI element has been removed. Per-decision draft confidence remains available.
- **Subjective draft labels such as VALUE, REACH, CORE, or CEILING.** Once versioned ADP exists, the
  API can expose the objective pick-versus-ADP delta without persisting editorial labels.
- **Fabricated live game phase and players-played counters.** Matchup status, score, projection, and
  NFL kickoff are authoritative; a display such as `Q3 08:24` needs a real play-by-play provider.
- **A fixed weekly action calendar and countdown.** The scheduler is deadline- and kickoff-driven.
  The UI should render `league/actions` rather than promise fixed weekday times that can drift.
- **Team colors, model logos, headshots, and editorial labels.** These are presentation assets and
  belong in the frontend. Team name and model identity remain backend-owned league data.

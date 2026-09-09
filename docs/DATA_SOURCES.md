# NFL data readiness

The backend uses public, unauthenticated feeds; no additional API key is required.

| Data | Source | App refresh |
| --- | --- | --- |
| Players and injury designations | Sleeper `/v1/players/nfl` | Players daily; injuries hourly during the season |
| Sleeper-to-GSIS identities | DynastyProcess `files/db_playerids.csv` (the nflreadr fantasy-ID source) | Daily and before scoring |
| Kickoffs and final scores | nflverse/nfldata `data/games.csv` | Every five minutes |
| Individual and defense statistics | nflverse `stats_player_week_{season}.csv` | Hourly during the season |

Scoring is **post-game**, not a live play-by-play service. nflverse publishes player
statistics after game days, so an hourly successful poll does not imply new scores
are available. A new season's file can return 404 before its first publication;
the scoring job reports `awaiting_stats_publication` and tries again next hour.
Other HTTP errors or unexpected schemas fail the job rather than writing empty data.

The schedule controls each player's kickoff lock independently of stats publication.
Weekly results require every scheduled regular-season game to be final and statistics
to include both teams from every game. Partial published weeks can update scores but
cannot advance the league. NFL postseason rows are excluded: fantasy playoff weeks
still refer to NFL regular-season weeks.

Player identity enrichment preserves Sleeper health and team metadata. Conflicting
IDs are logged and skipped rather than merging distinct players. Injury refreshes
include healthy records so removed designations clear in the database.

Operational checks: `/ready`, recent `nfl_schedule_sync`, `nfl_player_sync`,
`nfl_injury_sync`, and `nfl_stats_scoring` job results, plus GSIS coverage for rostered
non-defense players. Team defenses resolve by NFL team abbreviation.

Upstream documentation:

- https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html
- https://nflreadr.nflverse.com/reference/load_ff_playerids.html
- https://docs.sleeper.com/

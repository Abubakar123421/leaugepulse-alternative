# Current Product Context

Last updated: 2026-07-25

## Goal

Deliver a portable Discord league-management bot for Madden and College Football. One Discord application may serve multiple servers, but all settings and records must remain isolated by `guild_id` and, where applicable, `season`.

The deployment target is bot-hosting.net. The repository is the source of truth and must never contain live credentials or a live SQLite database.

## Confirmed Madden design

- NeonSportz is not connected as an API. The bot uses prepared roster and fixture CSV snapshots.
- The supplied Madden roster snapshot contains 32 franchises and 2,074 players.
- The supplied fixture snapshot contains 272 regular-season games across Weeks 1–18.
- The imported names include Black Knights (DEN), Condors (NE), and Dragons (DAL).
- Team ownership lasts for the entire season.
- Members can request an available franchise from its Open Teams **Claim Team** button. Commissioners approve or deny the pending request from persistent audit controls.
- Commissioners can use `/assign-team` for direct assignment, or `replace_existing: True` for a mid-season replacement. Replacement updates unfinished matchup ownership while preserving completed games and history.
- Commissioners can atomically assign 18–32 members with `/importmembers` using Discord IDs/usernames and team names.
- `/setup` never creates permanent channels or categories. It saves metadata/roles and preserves existing destination mappings.
- Permanent outputs are linked through `set...channel` commands and can be remapped after channel reorganization.
- Every active week uses its own top-level `WEEK X MATCHUPS` category. The bot creates the next week before deleting the old one. There is no permanent Weekly Matchups category.
- Matchup messages are not pinned.
- Scheduling and result submission remain reaction-driven. General disputes use a persistent button and private modal; the reason is posted only to commissioner audit.
- Unresolved games remain in SQLite after their Discord channels are archived and can be decided later from `/week`.
- Official results, owner history, career records, XP, recaps, and audit records survive weekly cleanup.
- Team emoji mappings store names, not Discord IDs. Replacing an emoji with the same name requires no database change.
- Game previews are not automatically posted in matchup channels. Final recaps and weekly content remain available.
- AI and streaming integrations are optional and cannot block league operations.

## Recommended Madden channel routing

Channel names are configurable; do not hardcode their IDs.

- Announcements: `#announcements`
- Final scores: `#final-scores`
- Weekly content: `#weekly-recaps`
- Trades: `#trade-block`
- Open Teams roster directory: `#open-teams-list`
- Polls/Game of the Week: `#game-of-the-week`
- Transactions: `#roster-moves`
- Streams: `#live-now`
- Private audit: `#commissioner-audit`
- Recruiting: optional/unused in Madden

## Reliability invariants

- Final statuses: `complete`, `force_home`, `force_away`, `fair_sim`.
- Player result evidence is private and commissioner-reviewed.
- Output posts use unique source keys/message IDs to prevent duplicates after retries and restarts.
- Roster, fixture, and member imports are previewed and atomic.
- Weekly rollover is restart-safe and guarded against concurrent runs.
- Failed Discord cleanup remains retryable.
- /season-force-delete may discard only the active test season after exact-text and button confirmation; other guilds, configured destinations, team-role definitions, and older archives remain untouched.
- Persistent views use stable custom IDs and are restored on startup.
- Gemini receives only sanitized public league facts, never secrets, evidence, audit content, or private dispute text.

## Current implementation

Implemented modules include configurable destinations, full-season roster/fixture imports, Open Teams roster cards, self-claim and CSV ownership, team roles, matchup categories/channels, adaptive reminders, result evidence/review, commissioner cases, final-score history, profiles/XP, season close/cleanup, confirmation-protected test-season force deletion, runtime emojis, Game of the Week voting, weekly recaps/rankings, awards, Gemini jobs, and optional stream alerts.

The automated suite currently contains 54 passing tests before release cleanup. Always rerun the suite after changes rather than relying on this number.

## Deployment state

The Git repository must ship without `data/leaguebot.sqlite3`. On first startup, the schema and parent directory are created automatically. The operator then runs `/setup`, maps existing permanent channels, imports the roster snapshot, assigns owners, and imports fixtures.

Read `AGENTS.md` first in future coding sessions. Use `IMPLEMENTATION.md` for module ownership and `docs/OPERATOR_GUIDE.md` for the live command sequence.

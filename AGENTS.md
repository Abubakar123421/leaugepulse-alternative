# Discord League Bot Agent Guide

Read this file first. Then read `README.md`, `CONTEXT.md`, and the specific source/tests relevant to the requested change. Do not restart the project from scratch.

## Product state

This is a production-oriented Python Discord bot for Madden and College Football leagues. One bot process can serve multiple Discord guilds. Every setting and business record must remain guild-scoped; season-specific records must also remain season-scoped.

The Madden delivery currently uses two prepared CSV snapshots rather than a live NeonSportz connection:

- `output/neonsportz-derived/madden_team_rosters.csv`: 32 franchises and 2,074 players.
- `output/neonsportz-derived/madden_18_week_fixtures.csv`: 272 fixtures across Weeks 1–18.

## Current operating model

- `/setup` saves league metadata, Commissioner role, and team roles. It must not create permanent Discord channels or permanent categories.
- Commissioners map existing server channels with the `set...channel` commands. `/destinations` displays the guild-specific routing.
- `/importrosters` imports the full roster snapshot.
- `/setopenteamlist` posts a roster directory: one team card with **View Team** per franchise. Claims happen only through `/registerteam`.
- `/importmembers` supports atomic CSV ownership assignment; `/registerteam` supports immediate self-service claims.
- Team ownership lasts for the entire season.
- `/importfixtures` imports the complete 18-week schedule. Only the active week has Discord channels.
- Each week gets its own top-level `WEEK X MATCHUPS` category. Rollover creates the next week first, then deletes the previous week’s category/channels. Do not replace this with a permanent matchup category.
- Matchup cards are not pinned. Scheduling/results use reactions; a persistent **Report Dispute** button opens a private reason form and posts a numbered case to commissioner audit.
- Official results post once to the configured final-score channel and update permanent history, records, and XP.
- Gemini is optional. AI failures must never block league operations.
- Team custom emojis are resolved by name at runtime; never persist or hardcode Discord emoji IDs.

## Key files

- `leaguebot/bot.py`: client lifecycle and slash commands.
- `leaguebot/db.py`: additive SQLite schema and migrations.
- `leaguebot/channel_workflow.py`: weekly categories, matchup channels, reactions, reminders, and disputes.
- `leaguebot/league_import.py`: full roster/fixture imports.
- `leaguebot/member_import.py`, `leaguebot/ownership.py`: ownership onboarding and mutation.
- `leaguebot/open_teams_ui.py`: roster directory and paginated team viewer.
- `leaguebot/result_ui.py`, `leaguebot/commissioner_ui.py`: evidence and official decisions.
- `leaguebot/services.py`: reminders, timed rollover, and stream polling.
- `leaguebot/team_emojis.py`: runtime emoji-name resolution.
- `leaguebot/weekly_content.py`, `leaguebot/awards.py`, `leaguebot/ai.py`: recaps, rankings, awards, and Gemini.
- `tests/`: regression and acceptance suite.

## Invariants

- Never mix data between guilds or seasons.
- Final matchup states are `complete`, `force_home`, `force_away`, and `fair_sim`.
- Player results remain pending until the opponent/commissioner workflow resolves them.
- Do not duplicate final-score, recap, Game of the Week, award, reminder, or AI posts after retries/restarts.
- Preserve completed history when ownership, active channels, or seasons change.
- Roster/member imports are all-or-nothing after preview validation.
- Persistent Discord controls require stable custom IDs and startup restoration.
- Never send secrets, evidence images, private disputes, or audit text to Gemini.
- Never commit `.env`, SQLite databases, logs, PIDs, caches, or backups.

## Verification

Use the bundled/local Python runtime available in the environment:

```powershell
python -m compileall -q leaguebot tests scripts
python -m pytest -q
```

For a clean-start smoke test, point `DATABASE_PATH` at a temporary file, initialize `Database`, and confirm the schema is created. Live verification scripts under `scripts/verify_live_*.py` are read-only but require a valid local `.env` and an existing test database.

## Deployment

Target host: bot-hosting.net.

- Entry file: `main.py`
- Python: 3.11 or newer
- Runtime dependencies: `requirements.txt`
- Secrets: host environment variables copied from `.env.example`
- SQLite path: `data/leaguebot.sqlite3`

The repository is intentionally shipped without a database. The bot creates a fresh database and `data/` directory on first startup. Configure permanent Discord destinations after `/setup`.

# Implementation Handoff

## Read order

1. `AGENTS.md`
2. `README.md`
3. `CONTEXT.md`
4. This file
5. The relevant module and tests

Do not recreate the project or replace working modules wholesale.

## Runtime architecture

- Python 3.11+
- `discord.py` persistent views and application commands
- `aiosqlite` with additive migrations
- One long-running process serving multiple guilds
- Optional `google-genai` Gemini client
- Optional Twitch/YouTube polling through `aiohttp`

`main.py` calls `leaguebot.bot.run()`. `LeagueBot.setup_hook()` initializes/migrates SQLite, registers persistent dynamic controls, syncs slash commands, and starts AI, reminders, week rollover, and stream services.

## Module map

- `bot.py`: client, startup, slash commands, destination setters.
- `db.py`: schema, migrations, guild settings, audit helpers.
- `league_import.py`: complete roster and fixture imports.
- `member_import.py`: atomic owner CSV validation/application.
- `ownership.py`: centralized claim/release/role/matchup mutation.
- `open_teams_ui.py`: roster directory and paginated viewer.
- `channel_workflow.py`: weekly category lifecycle, matchup cards/reactions, prompts, disputes.
- `schedule_ui.py`: versioned private scheduling decisions.
- `result_ui.py`: evidence uploads, opponent decisions, commissioner approval, final posts.
- `commissioner_ui.py`: weekly dashboard and guarded outcomes.
- `services.py`: reminder, timed rollover, and stream workers.
- `season_lifecycle.py`, `season_ui.py`: archive and retryable cleanup.
- `team_emojis.py`: emoji-name aliases and runtime resolution.
- `weekly_content.py`, `awards.py`, `game_of_week.py`, `ai.py`: generated content and idempotency.
- `progression.py`: career statistics and XP.

## Data model highlights

- `guild_settings`: per-server configuration and destination IDs.
- `franchises`, `roster_players`: imported season snapshot.
- `profiles`, `open_rosters`, `season_participants`: current ownership and season participation.
- `matchups`, `week_categories`, `week_rollover_jobs`: full schedule and active Discord lifecycle.
- `matchup_prompts`, `matchup_cases`, `reminder_deliveries`: interaction/reliability state.
- `game_history`, `ownership_history`, `career_profiles`, `xp_events`, `season_archives`: permanent history.
- AI/content tables persist source keys and message IDs for idempotent posting.

All tables that represent server data must be guild-scoped. Season-sensitive data must also be season-scoped.

## Core flows

### Fresh setup

`/setup` saves league metadata and Commissioner role and creates/reuses team roles. It intentionally creates no permanent channels or categories. The operator maps existing channels with destination commands.

### Season onboarding

1. `/importrosters` imports the full prepared roster snapshot.
2. `/setopenteamlist` posts the roster directory.
3. Owners use `/registerteam`, or a commissioner confirms `/importmembers`.
4. `/importfixtures start_now:True` imports all weeks and creates Week 1.

### Weekly lifecycle

`create_week_matchup_channels()` creates a top-level `WEEK X MATCHUPS` category and fixture channels. Timed/manual advancement creates the next week before deleting the old category. Unresolved database records remain available to `/week`.

### Results and disputes

Score submissions require evidence. Opponent/commissioner controls are versioned. Official results are idempotently posted to final scores and recorded in progression/history. The matchup card’s **Report Dispute** button opens a reason modal and posts a private numbered case without requiring an existing score submission.

## Release checks

```powershell
python -m compileall -q leaguebot tests scripts
python -m pytest -q
```

Also verify:

- `.env` is ignored and untracked.
- No `data/*.sqlite3`, backup, log, PID, cache, or virtual environment is tracked.
- A temporary empty database initializes successfully.
- `requirements.txt` contains runtime dependencies only; development tools are in `requirements-dev.txt`.
- `git diff --check` is clean.

## Deployment

bot-hosting.net reads root `requirements.txt`; set the entry file to `main.py`. Configure secrets using the host environment-variable panel. The first startup creates `data/leaguebot.sqlite3`. Back it up before any later replace-style source sync.

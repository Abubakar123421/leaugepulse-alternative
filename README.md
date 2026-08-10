# Discord League Bot

A multi-server Discord bot for Madden and College Football leagues. Guild settings, owners, rosters, fixtures, results, channel destinations, reminders, AI jobs, and career records are isolated by Discord server and season.

## Madden workflow

The bot uses prepared CSV snapshots; it does not connect directly to NeonSportz.

1. `/setup` saves the league name, game, season, timezone, Commissioner role, and creates/reuses team roles. It does **not** create permanent channels or permanent categories.
2. Commissioners map existing server channels with the destination commands. `/destinations` shows the saved routing.
3. `/importrosters` imports `output/neonsportz-derived/madden_team_rosters.csv` (32 franchises, 2,074 players).
4. `/setopenteamlist` posts one roster card per franchise with private paginated **View Team** and **Claim Team** controls. Claims remain pending until a commissioner uses the persistent Approve/Deny review buttons. Commissioners can directly assign or replace one owner with `/assign-team`, or bulk-assign owners with `/importmembers`.
5. `/importfixtures` imports `output/neonsportz-derived/madden_18_week_fixtures.csv` (272 fixtures, Weeks 1–18). Team ownership lasts for the full season.
6. The active week gets a top-level `WEEK X MATCHUPS` category and one channel per fixture. Rollover creates the next week first, then deletes the previous week’s channels/category. Results and history remain in SQLite.

Matchup cards are not pinned. Owners use reactions to schedule, counter, confirm, submit a score/screenshot, and request rulings. **Report Dispute** opens a private form and sends a numbered case to commissioner audit. Official results post exactly once to the configured final-score channel.

## Permanent destination commands

- `/setannouncementchannel`
- `/setscorechannel`
- `/setstorylinechannel`
- `/settradechannel`
- `/setopenteamlist` (`/setopenchannel` alias)
- `/setpollchannel`
- `/settransactionchannel`
- `/setstreamchannel`
- `/setauditchannel`
- `/setrecruitingchannel` (primarily College Football)

Weekly matchup categories are automatic and are not mapped to a permanent category.

## Other major features

- Atomic `/importmembers` CSV onboarding, direct `/assign-team` assignment/reassignment, plus `/syncmemberroles` repair.
- Runtime custom-team-emoji resolution by emoji name via `/syncteamemojis` and `/setteamemoji`; Discord emoji IDs are never hardcoded.
- Commissioner dashboards, force wins, fair simulations, result evidence/review, team release, and restart-safe reminders.
- Career profiles, XP, leaderboard, season history, archival, and retryable season cleanup.
- Confirmation-protected `/season-force-delete` for discarding an active demo/test season without affecting other servers or prior archives.
- Game of the Week graphics with team-emoji voting.
- Deterministic rankings/weekly recap facts, season awards, and optional Gemini narrative generation.
- Optional Twitch and YouTube live alerts.

## Local development

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Add secrets to .env locally.
python -m compileall -q leaguebot tests scripts
python -m pytest -q
python main.py
```

Invite the bot with `bot` and `applications.commands`. Enable Server Members intent. The bot needs View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Manage Roles, and Manage Channels. Keep its role above all team roles.

## Fresh bot-hosting.net deployment

The repository intentionally contains no live database or `.env`.

1. Create an **Application** deployment using the GitHub repository or a ZIP.
2. Choose Python 3.11 or newer.
3. Set **Entry File (`STARTUP_FILE`)** to `main.py`.
4. Keep `requirements.txt` in the repository root; the host installs it automatically.
5. Add the variables from `.env.example` in the host’s Environment Variables tab. `DISCORD_TOKEN` is required; Gemini, Twitch, and YouTube are optional.
6. Start the deployment. The bot creates `data/leaguebot.sqlite3` automatically.
7. Run `/setup`, map the permanent channels, import rosters, and import fixtures.

For later GitHub updates, back up `data/leaguebot.sqlite3` first and use a merge-style sync so host-only runtime data is not removed.

See [the hosting guide](docs/HOSTING_TRANSFER.md), [operator guide](docs/OPERATOR_GUIDE.md), and [agent handoff](AGENTS.md).

## Security

Never commit `.env`, Discord tokens, API keys, SQLite databases, logs, PIDs, or backups. Rotate any token that has ever been exposed. Production secrets belong only in the host’s environment-variable manager.

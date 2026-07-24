# Discord League Bot

A portable, multi-server Discord bot for Madden and College Football leagues. Every league setting, owner, roster, fixture, result, channel destination, and career record is isolated by Discord server.

## Current Madden workflow

The bot uses exported league data as an import snapshot; it does not connect to NeonSportz.

- `/importrosters` imports the prepared 32-team roster snapshot and creates/reuses the matching team roles.
- `/setopenteamlist` posts one persistent card per franchise with owner status, a roster preview, **Claim Team**, and a private paginated **View Team** button.
- `/importfixtures` imports the complete season schedule. The supplied Madden file contains 272 fixtures across Weeks 1–18.
- Team ownership lasts for the entire season. Members claim an open team with `/registerteam` or the persistent Open Teams panel; commissioners may use `/importmembers` for bulk assignments.
- Starting the fixture import creates only the `WEEK 1 MATCHUPS` category and Week 1 game channels.
- Every seven days, the bot creates the next week's category and channels first, then removes the previous weekly category and channels.
- Results, records, ownership, career XP, and unresolved commissioner cases remain in SQLite after weekly channels are removed.
- Unresolved games do not stop timed rollover. Commissioners can review an archived week with `/week number:<week>` and issue a force win, fair sim, or other final decision later.
- Discord permission/API cleanup failures are retained and retried automatically.
- `/roster team:<name-or-abbreviation>` displays a team's imported roster, and `/playersearch player:<name>` searches the season snapshot.
- Team logos are not required and the player logo-upload workflow is disabled.

Prepared imports:

- `output/neonsportz-derived/madden_team_rosters.csv` — 32 teams and 2,074 players.
- `output/neonsportz-derived/madden_18_week_fixtures.csv` — 272 unplayed fixtures across 18 weeks.

The imported franchise set includes the custom Black Knights, Condors, and Dragons names from the supplied data.

## Commissioner setup order

1. Run `/setup` and choose the league settings and Commissioner role.
2. Configure output channels with `/setannouncementchannel`, `/setscorechannel`, `/setstreamchannel`, `/setstorylinechannel`, `/settradechannel`, `/settransactionchannel`, `/setauditchannel`, `/setopenteamlist`, `/setpollchannel`, and `/setrecruitingchannel` as needed.
3. Run `/importrosters` with `madden_team_rosters.csv`, review the 32-team preview, and confirm.
4. Let members claim teams, or run `/importmembers` with Discord IDs/usernames and team names.
5. Run `/importfixtures` with `madden_18_week_fixtures.csv`. Keep `start_now:True` to start the seven-day Week 1 clock and create Week 1 channels.
6. Use `/week` for the commissioner dashboard. Use `/roster` and `/playersearch` to demonstrate imported data.

`/destinations` shows the complete per-server routing table. Madden and College Football servers can use different channels and settings while one bot application serves both.

## Matchup experience

Each active fixture gets one public read-only channel. Only its two team owners and commissioners can interact. The matchup card is not pinned and supports reactions for scheduling, counterproposals, confirmation, score/screenshot submission, disputes, force-win requests, fair sim, and commissioner help.

Scheduling reminders are restart-safe:

- More than 24 hours remaining: once daily.
- Between 24 and 6 hours: every two hours.
- Final 6 hours: hourly.
- Reminders stop immediately when the game is scheduled.

Result evidence and cases go to the private commissioner audit channel. Official results post to the configured final-score channel and permanently update records and career progression.

## AI and streams

Gemini can generate matchup previews, final recaps, weekly storylines, power-ranking narratives, and commissioner-nominated Player of the Week posts. Rankings are calculated deterministically; Gemini writes only the narrative. AI failures never block league operations.

Twitch and YouTube alerts are optional. Leave their credentials blank to run without stream detection.

## Local setup

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Invite the bot with the `bot` and `applications.commands` scopes. Enable Server Members intent. It needs View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Manage Roles, and Manage Channels. Keep the bot role above team roles.

Never commit or transfer a real `.env` file. If a bot token was exposed, rotate it in the Discord Developer Portal.

## Tests

```powershell
python -m compileall leaguebot tests scripts
python -m pytest -q
```

The current suite also verifies the real prepared imports at 32 teams, 2,074 players, 272 fixtures, 18 weeks, and tests rollover while unresolved games remain saved.

## Deployment and transfer

See [docs/HOSTING_TRANSFER.md](docs/HOSTING_TRANSFER.md) and [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md). Keep `data/` persistent and download backups regularly.
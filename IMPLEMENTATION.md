# Discord League Bot - Implementation Handoff

## Instructions for the Next Codex Session

Read these files first:

1. `CONTEXT.md`
2. `IMPLEMENTATION.md`
3. `README.md` when present
4. `.env.example`
5. The source under `leaguebot/`
6. The tests under `tests/`

Continue implementing and verifying the existing project. Do not restart from scratch, replace working code wholesale, or add real secrets. Preserve the confirmed scope in `CONTEXT.md`.

## Technology Decisions

- Python 3.11+.
- discord.py 2.6.
- aiosqlite with a portable SQLite database.
- aiohttp for Twitch/YouTube requests.
- python-dotenv for local secret loading.
- reportlab for the client PDF.
- pytest/pytest-asyncio for automated tests.
- One process and one Discord application serving multiple guilds.
- All business records are keyed by `guild_id`; season-sensitive records also include `season`.

## Current Project State

Created:

- Project structure.
- `requirements.txt`.
- `.env.example`.
- `.gitignore`.
- `main.py`.
- `leaguebot/config.py`.
- `leaguebot/db.py`.
- `leaguebot/imports.py`.
- `leaguebot/helpers.py`.
- `leaguebot/help_ui.py`.

Implemented so far:

- Environment configuration.
- SQLite schema for guild settings, teams, profiles, matchups, reminder deliveries, transactions, open rosters, stream alert state, and audit logs.
- Database initialization and settings/audit helpers.
- Flexible CSV parsing and validation with column aliases.
- Stable external game keys for duplicate-safe re-imports.
- Timezone/deadline and commissioner helpers.
- Interactive `/help` view foundation that reads live slash-command metadata, filters commissioner commands and disabled features, and supports category/command selects plus Home/Previous/Next.

Not yet complete:

- Discord bot bootstrap and command registration.
- `/setup` wizard and settings UI.
- `/register`, approval, and profile display.
- Matchup persistent views and modals.
- Import preview/confirmation and Discord thread creation.
- Commissioner dashboard and audit channel posting.
- Reminder scheduler.
- Twitch/YouTube pollers.
- Trades, transfers, open rosters, announcements, awards.
- Database backup command.
- Tests, README, operator guide, hosting-transfer guide.
- Client PDF generation and visual verification.

Update this section whenever a meaningful implementation slice is completed.

## Required Command Surface

- `/setup`
- `/help`
- `/settings`
- `/register`
- `/profile`
- `/profile-approve`
- `/import-schedule`
- `/team-logo`
- `/week`
- `/announce`
- `/award`
- `/trade-block`
- `/transaction`
- `/open-roster`
- `/backup`

Command names may use Discord-compatible hyphens. Keep the help metadata synchronized with registered command descriptions.

## Data and Behavior Invariants

- Never mix records between Discord guilds.
- Treat `complete`, `force_home`, `force_away`, and `fair_sim` as final statuses.
- Player-submitted results are pending until commissioner approval.
- Do not advance a week with unresolved matchups.
- Store every reminder milestone once per matchup.
- Re-import the same game by `(guild_id, season, week, external_key)` and update it in place.
- Never create matchup channels until the preview is confirmed.
- Commissioner actions must be permission checked and audit logged.
- User team claims require commissioner approval.
- Important component custom IDs must remain stable so persistent views survive restarts.
- Feature-disabled commands must reject use clearly and disappear from `/help`.

## Verification Commands

Use the project virtual environment if one exists. On Windows:

```powershell
python -m pip install -r requirements.txt
python -m compileall leaguebot tests
python -m pytest -q
```

Before running the live bot:

```powershell
Copy-Item .env.example .env
# Add secrets to .env manually and securely.
python main.py
```

Do not put a real token into tests or documentation.

## PDF Requirement

Generate:

`Discord_League_Bot_Project_Plan.pdf`

The PDF must:

- Use client-friendly language rather than implementation jargon.
- Include every confirmed current feature, including `/help`.
- Clearly separate current delivery from future expansion.
- State that Josh creates the College Football server but does not need to create its channels.
- Exclude credentials, technical secrets, AI features, pricing, and unconfirmed promises.
- Be rendered to PNG using Poppler and visually inspected for clipping, overlap, missing glyphs, page breaks, headers, footers, and page numbering.

Keep the PDF-generation script in `scripts/` so it can be reproduced on another PC.

## Deployment Requirement

bot-hosting.net reads Python packages from `requirements.txt`. The final project must include:

- Startup file `main.py`.
- `.env.example`.
- Persistent `data/` directory and backup guidance.
- No virtual environment or cache folders.
- No real `.env` file in the transfer package.
- README instructions for uploading a ZIP or Git repository and setting the startup file.


## Implementation Status - 2026-07-22

Implemented in this workspace:

- Python 3.11-compatible project bootstrap and environment configuration.
- Portable, guild-isolated SQLite schema, audit records, and online backup support.
- All 15 required slash commands and interactive help navigation.
- Setup/channel creation, league settings, feature toggles, registration approval, and profiles.
- CSV validation, preview, confirmed duplicate-safe imports, week dashboard, and matchup channels.
- Persistent matchup buttons for scheduling, pending score submission, commissioner requests, and issue reports.
- Guarded commissioner outcomes and blocked week advancement while games remain unresolved.
- Restart-safe reminder delivery at no-response and deadline milestones.
- Twitch and YouTube live polling when the relevant credentials and channel identifiers are configured.
- Announcements, awards, trade block, transactions/transfers, and Open Rosters/Open Teams.
- Tests, client/operator documentation, hosting-transfer guidance, schedule template, and reproducible client PDF.

Verification completed with Python 3.12 while also parsing every source file with the Python 3.11 grammar. All automated tests pass. Live Discord acceptance testing still requires a bot token, test server, role/channel permissions, and the client CSV/logo samples listed in `CONTEXT.md`.
## Madden End-Product Hardening - 2026-07-23

Implemented and verified:

- Private, versioned scheduling proposals with Accept, Counter, and Decline; stale buttons and concurrent decisions cannot overwrite newer state.
- Player availability cases, deadline extensions, reopened scheduling, commissioner-visible no-show/issue cases, and commissioner-approved concessions.
- Evidence-based result submission with opponent confirmation/dispute, commissioner approval, versioned controls, and reversible active-season finals.
- Permanent career profiles, season ownership records, idempotent XP events, levels, win rate, force-win/forfeit/sim counters, championships, `/leaderboard`, and `/season-history`.
- Safe `/team-release` for replacements and departed members without erasing prior ownership or career history.
- Guarded `/season-close`: unresolved-game block, second confirmation, compact official history, operational database cleanup, SQLite compaction, new channel creation before old-channel deletion, and retryable partial cleanup.
- Normal `/week` is read-only; commissioners receive the private interactive dashboard. Missing matchup channels are created under a per-week lock.

XP rules: played win 100, played loss 50, force win 60, fair sim 15 per assigned player, championship 500. Forfeits do not count as games played and award no loser XP.

Verification: the complete suite passes (31 tests), all source/test modules compile under the bundled Python runtime, the live database migration reports `integrity: ok`, and JanthoBot restarted successfully with Discord command sync and one connected guild. A pre-migration backup is stored under `data/backups/`.
## LeaguePulse-Style Weekly Redesign - 2026-07-23

Implemented:

- 32 idempotent Madden team roles, approval/release assignment, and season-rollover clearing without role duplication.
- Registration from the full NFL catalog before schedule import; exact spelling with case-insensitive matching.
- One-current-week CSV enforcement and NFL team validation, with unrestricted positive week numbers.
- Dedicated public/read-only matchup channels with owner/commissioner write permissions and pinned reaction cards.
- Reaction-driven scheduling, counters, confirmations, screenshot result submission, disputes, force-win/fair-sim/help cases, and unauthorized reaction removal.
- Restart-safe adaptive scheduling reminders and private missing-owner alerts.
- Official-result channel locking, weekly channel deletion on advancement, season category cleanup, and permanent compact history.
- Commissioner final-result/ruling publishing moved off the legacy shared matchup/thread path.

Verification: 34 automated tests pass and every leaguebot module compiles under the bundled Python runtime.
## Configurable Destination Routing - 2026-07-23

Implemented guild-isolated destination IDs and setter commands for matchup categories,
announcements, official scores, live streams, weekly spotlight content, trades,
transactions/transfers, open teams, polls, recruiting, and commissioner audit events.
`/destinations` shows the current routing table and `/createweek` creates missing weekly
channels idempotently. Active matchup channels move when the category changes. Season
rollover preserves configured destinations. Existing automated announcement, award,
score, stream, trade, transaction, open-team, audit, and matchup outputs use these fields.
## Implemented: imported rosters, full fixtures, and timed weekly rollover (2026-07-23)

- Added additive SQLite tables for franchises, roster players, weekly category ownership, and idempotent rollover jobs.
- Added `/importrosters`, `/importfixtures`, `/roster`, and `/playersearch`.
- Imported team identity now drives claims, member CSV validation, roles, matchup ownership, permissions, reminders, and AI inputs.
- Full-season fixtures are stored at once; only the active week creates Discord channels.
- Automatic seven-day rollover creates the next week first, retains unresolved game records, deletes the old weekly category, retries cleanup failures, and preserves permanent outcomes/history.
- Disabled the Discord team-logo prompt/display path.
- Added acceptance tests against the prepared 32-team, 2,074-player, 272-game, 18-week CSV snapshots and an unresolved-game rollover test.

# Discord League Bot - Transfer Context

## Purpose

This repository contains a paid Discord league-management bot being built for a Fiverr client named Josh (`Jantho1994` / `joshanthony1994`).

The bot will be hosted by the developer on bot-hosting.net and must be portable so Josh can receive the complete source code, database, configuration template, backups, and hosting-transfer instructions.

Do not put real Discord tokens, Twitch secrets, YouTube keys, passwords, or client personal information in this repository.

## Client Goal

Josh runs private EA Sports leagues:

- Madden 26, with reuse for Madden 27 and later seasons.
- College Football 27 and later seasons.
- The leagues use separate Discord servers.
- One Discord bot application must serve both servers while isolating all data by Discord guild.

Josh will create the College Football server. He does not need to create its channels because `/setup` will create or connect them.

## Confirmed Current Scope

### Setup and settings

- `/setup` creates or links the league category and required channels.
- Each guild has independent league name, game, season, week, timezone, advance day/time, channels, commissioner role, reminders, and feature toggles.
- Interactive League Settings panel.

### Profiles and streams

- `/register` stores a Discord member's requested team, Twitch, and YouTube information.
- A commissioner approves team claims.
- A single user may have different teams in different guilds.
- Approved stream links appear in matchup posts.
- Twitch and YouTube live alerts post automatically when credentials are configured.

### Guild-scoped output routing

- Every generated output resolves its destination from `guild_settings`; no channel IDs are hardcoded.
- Commissioners configure matchups, announcements, final scores, streams, weekly spotlight, trades, transactions, open teams, polls, recruiting, and audit destinations with slash commands.
- `/destinations` is the routing overview and `/createweek` is the idempotent weekly channel creator.
- Changing the matchup category moves active channels; season rollover preserves configured destination channels and removes season-owned matchup channels only.
### Madden imported-data and weekly channel workflow

- NeonSportz is not connected as an API or live data source. The bot consumes two prepared CSV snapshots derived from the client export.
- `/importrosters` atomically imports 32 franchises and 2,074 roster players for the active guild/season. Team roles, Open Teams, member imports, claims, matchup permissions, reminders, and AI mappings use those imported franchise identities.
- `/importfixtures` atomically imports all 272 regular-season fixtures across Weeks 1–18. Scores from the source export are intentionally removed; the Discord league records its own official outcomes.
- Team claims last for the entire season. They are not reset during weekly rollover.
- The Open Teams channel contains a pinned summary plus one persistent franchise card per team. Cards show owner/open status and a five-player roster preview, with atomic **Claim Team** and ephemeral paginated **View Team** buttons. Cards restore after bot restarts and refresh after imports, claims, approvals, releases, and season changes.
- Starting the season creates only the `WEEK 1 MATCHUPS` category. Every seven days the bot creates the next week's category/channels first and deletes the old weekly category/channels.
- Unresolved games never disappear from SQLite and do not block timed rollover. Commissioners can use the archived `/week number:<n>` dashboard to issue later decisions. Archived dashboards do not recreate deleted channels.
- Weekly rollover is idempotent, restart-safe, guarded against simultaneous execution, and keeps the current week intact if the next week cannot be fully created. Old-channel cleanup failures are retried automatically.
- Only both assigned team roles and commissioners can send/react in active matchup channels. Everyone else has read-only access.
- Player controls remain reaction-driven: schedule, counter, accept, submit result+screenshot, dispute, home/away force-win request, fair sim, and commissioner help.
- Scheduling reminders are restart-safe: daily above 24h, every two hours from 24–6h, hourly in the final six hours, and stop immediately once scheduled.
- `/roster` and `/playersearch` expose the imported roster snapshot. Discord team-logo uploads are no longer required or displayed.
- Commissioner evidence/cases go to the private audit channel. Official results, records, game history, owner history, and career XP survive weekly channel deletion.
### League modules

Madden:

- Trade tracking.
- Trade block.
- Open Rosters/Open Teams.

College Football:

- Transfer tracking.
- Open Rosters/Open Teams.

Both:

- Announcements.
- Game of the Week.
- Players of the Week.
- Twitch and YouTube alerts.
- Per-module on/off controls.

Trade/transfer tracking covers data entered or imported into this bot. Do not promise undocumented EA or NeonSportz API access.

### Interactive help

`/help` opens an ephemeral help center with:

- Category and command select menus.
- Getting Started.
- Player Registration.
- Matchups and Scheduling.
- Player Commands.
- Commissioner Commands.
- Schedule Imports.
- Trades and Transfers.
- Streams and Profiles.
- League Settings.
- Troubleshooting.
- Home, Previous, and Next buttons.
- Permission- and feature-aware command visibility.
- Details, examples, and outcomes for commands/forms.
- Automatic fallback entries generated from registered slash-command metadata.

## Madden Season Lifecycle and Careers

- Every official result records permanent, guild-isolated player career totals and an idempotent XP event.
- `/profile` shows career and season history; `/leaderboard` ranks XP; `/season-history` displays archived tournament results.
- `/season-close` refuses unresolved games, preserves compact results and team ownership, clears active-season data, rotates Discord channels, and deletes old threads/evidence.
- Cleanup state is recorded so `/season-cleanup` can recover safely from partial Discord permission or network failures.
- Team release and departed-member workflows preserve historical ownership while reopening only unfinished games.
## Explicitly Out of Current Scope

The architecture should allow these later, but they are not part of the current paid delivery:

- Power Rankings.
- Storyline posts.
- Recruiting posts.
- Playoff picture.
- AI-generated stories.
- Automatic game recaps.

## Information Still Needed

From Josh:

- College Football server and permission to install the bot.
- League names, timezone, default advance schedule, season, and current week.
- Commissioner accounts/role.
- Madden team assignments.
- A newly prepared roster snapshot and full fixture snapshot for each new season.
- College Football assignments and schedule when ready.
- Preferred channel names, if any.

From the developer/host:

- Discord application and bot token.
- bot-hosting.net deployment access.
- Twitch Developer client ID and secret.
- YouTube Data API key.
- Bot name, avatar, and branding.

## Project Location and Deliverables

Original Windows project directory:

`C:\Users\ABUBAKAR\Desktop\the bot`

The client PDF must also be copied to:

`C:\Users\ABUBAKAR\Documents\Codex\2026-07-22\amke\outputs\Discord_League_Bot_Project_Plan.pdf`

When transferring to another PC, copy the entire `the bot` folder, including this file and `IMPLEMENTATION.md`. Do not copy `.env` if it contains real secrets; recreate it securely from `.env.example`.


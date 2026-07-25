# Madden League Bot Client Demo

Prepare one commissioner account, two normal accounts, the supplied roster/fixture CSVs, one Game of the Week image, and one final-score screenshot.

## 1. Setup and destinations

Run `/setup` with the league name, `Madden 26`, season, timezone, and Commissioner role. Show that no permanent channels are created.

Map existing channels with the destination setters, then run `/destinations` to show that outputs can be moved without code changes.

## 2. Import league data

1. `/importrosters` with `madden_team_rosters.csv`; confirm the 32-team/2,074-player preview.
2. `/setopenteamlist` and show one roster card per team. Open **View Team** to demonstrate private pagination.
3. `/importfixtures` with `madden_18_week_fixtures.csv`, `start_now:True`; confirm 272 fixtures and Week 1 creation.

## 3. Assign owners

Have both players run `/registerteam` for available teams. Show immediate ownership, roles, Open Teams updates, and matchup permissions. Mention `/importmembers` as the commissioner’s bulk alternative.

## 4. Schedule a game

In the matchup channel, Player A reacts with the calendar and enters `YYYY-MM-DD HH:MM`. Player B accepts or counters. Show the localized confirmed time and that reminders stop once scheduled.

## 5. Report an issue

Click **Report Dispute**, enter a reason in the popup, and show the numbered private case in commissioner audit. It does not require a previously submitted score.

## 6. Submit and approve a result

An owner submits the away-home score and attaches a PNG/JPG/WebP screenshot. Show private evidence review, opponent confirmation/dispute, commissioner approval through `/week`, exactly one final-score post, and updated `/profile`/`/leaderboard` data.

## 7. Game of the Week

Run `/gameoftheweek`, enter the week first, choose one of that week’s matchup IDs, and attach artwork. Show the two team-emoji voting reactions in the configured Game of the Week channel.

## 8. Advance

Use the `/week` commissioner dashboard to advance. Show that the bot creates `WEEK 2 MATCHUPS` before deleting Week 1 Discord channels, while records and unresolved cases remain saved.

## 9. Reliability and transfer

Explain restart-safe reminders, idempotent output posts, automatic migrations, backups, multi-server isolation, and configurable destinations. The hosted release starts with no database and creates it automatically.

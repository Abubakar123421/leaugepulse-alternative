# League Bot Operator Guide

## First deployment

1. Deploy the repository with Python 3.11+ and entry file `main.py`.
2. Add `DISCORD_TOKEN` and optional integration variables from `.env.example` in the host environment manager.
3. Invite the bot with `bot` and `applications.commands`; enable Server Members intent.
4. Put the bot role above team roles and grant Manage Roles, Manage Channels, Manage Messages, View Channels, Send Messages, Embed Links, Attach Files, and Read Message History.

## First Discord setup

Run `/setup` with league name, game, season, timezone, and Commissioner role. This saves configuration and creates/reuses team roles; it does not create channels.

Map the server’s existing channels:

- `/setannouncementchannel`
- `/setscorechannel`
- `/setstorylinechannel`
- `/settradechannel`
- `/setopenteamlist`
- `/setpollchannel`
- `/settransactionchannel`
- `/setstreamchannel`
- `/setauditchannel`
- `/setrecruitingchannel` when needed for College Football

Run `/destinations` to verify the routing. Any mapping can be changed later by rerunning its setter. Weekly matchup categories are automatic and have no permanent destination.

## Madden season onboarding

1. `/importrosters` with `output/neonsportz-derived/madden_team_rosters.csv`; review and confirm.
2. `/syncteamemojis` if the server has team emojis.
3. Members claim from an Open Teams card, or a commissioner runs `/importmembers`. The recommended CSV header is `team,discord_id,twitch,youtube`; `discord_username` may replace `discord_id`, but IDs are more reliable.
4. Use `/syncmemberroles` if Discord role synchronization reported failures.
5. `/importfixtures` with `output/neonsportz-derived/madden_18_week_fixtures.csv`. Use `start_now:True` to create Week 1 and begin the seven-day clock.

## Weekly operation

- Each active week has a top-level `WEEK X MATCHUPS` category.
- Only assigned owners and commissioners can interact; everyone else is read-only.
- Reactions schedule/counter/confirm times, submit results, and request outcomes.
- **Report Dispute** opens a private form whose reason goes to commissioner audit.
- `/week number:<week>` opens the commissioner dashboard for approvals, force wins, fair simulations, reminders, issue review, and advancement.
- `/gameoftheweek week:<week> matchup_id:<choice> graphic:<image>` posts the vote graphic to the configured polls channel.
- Advancement creates the next week first and then removes the old Discord category/channels. Database history is retained.

If a Game of the Week picker appears empty, enter the `week` option first; the matchup picker filters to that selected week.

## Rosters and owners

- `/roster team:<team>` and `/playersearch player:<name>` query imported data.
- Open Teams cards provide **View Team** only. Claims use `/registerteam`.
- `/team-release` safely removes an owner, reopens eligible unfinished games, and preserves history.

## Results

Owners submit an away-home score with one PNG/JPG/WebP evidence image. The opponent can confirm or dispute; commissioners make the official decision. Approved/forced/sim outcomes post once to final scores and update records/XP.

## AI and streams

- `/aistatus` checks Gemini readiness without exposing the key.
- `/aisettings`, `/airecap`, `/aiweek`, and `/weekmvp` control generated content.
- Gemini is optional; failures never block gameplay.
- Twitch/YouTube credentials are optional. With blank credentials, the rest of the bot works normally.

## Season close and backups

1. Resolve all required games and complete/publish the eight season awards.
2. Run `/backup` and download the file outside the host.
3. Run `/season-close` and confirm.
4. If cleanup partially fails, fix Discord permissions and run `/season-cleanup`.

The bot preserves compact results, careers, XP, ownership history, and awards while clearing active operational data and team-role members.
### Force-delete a demo or test season

Use this only when the active season is disposable and must be reset even though games or awards are unfinished.

For ordinary schedule/channel testing, use `/season-test-reset confirmation:RESET <current season>` instead. It returns unfinished matchups to `waiting`, removes generated weekly channels and pending workflow data, and preserves completed results, career history, rosters, fixtures, and ownership.

1. Run `/backup` and download the database file.
2. Run `/season-force-delete new_season:<new name> confirmation:DELETE <current season>`.
3. Review the private deletion preview and press **Permanently Delete Test Season**.
4. Reimport rosters, repost the Open Teams directory, assign owners, and reimport fixtures.

This removes the active season's rosters, fixtures, owners, result evidence, tracked posts, weekly channels, recaps, awards, reminders, and XP earned in that season. It preserves configured permanent destination channels, the Commissioner role, empty team-role definitions, other Discord servers, and previously archived history. The old season name must match exactly in the confirmation text.

## Troubleshooting

- Missing slash commands: restart and confirm `applications.commands` was included in the invite.
- Role failures: move the bot role above team roles, then `/syncmemberroles`.
- Destination failures: rerun the relevant setter and ensure View Channel, Send Messages, and Embed Links.
- Missing opponent DM: Discord privacy can block DMs; the commissioner audit review remains available.
- Slow roster import: the confirmation is acknowledged immediately and roster cards refresh in the background.
- Before replacing host files: download/back up `data/leaguebot.sqlite3`.

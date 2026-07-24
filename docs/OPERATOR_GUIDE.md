# League Bot Operator Guide

## First-time setup

1. Invite the bot with the `bot` and `applications.commands` scopes. Enable Server Members intent.
2. Put the bot role above the league's team roles and grant Manage Roles, Manage Channels, Manage Messages, View Channels, Send Messages, Embed Links, Attach Files, and Read Message History.
3. Run `/setup` with the league, game, season, timezone, and Commissioner role. It creates/reconnects management channels and creates any missing 32 NFL team roles without duplicates.
4. Members run `/register` and choose one of the 32 available teams. A commissioner runs `/profile-approve`; approval grants the team role. Registration can happen before the first schedule import.

## Destination setup

Run `/destinations` after `/setup`. The default channels can be replaced at any time
with the `set…channel` commands, and weekly game placement is controlled by
`/setmatchcategory`. Official scores, announcements, streams, weekly spotlight content,
trades, transfers, open teams, polls, recruiting, and private audit events each have a
separate saved destination. Changes affect only the current Discord server.
## Weekly operation

1. Upload exactly one current-week CSV with `/import-schedule`. Required columns are `week,away_team,home_team`; optional columns are `away_user_id,home_user_id,game_id`.
2. Confirm the preview. The bot creates a `Week N` category and one channel per game. Everyone can read it; only both team roles and commissioners can send/react.
3. Owners use the pinned card:
   - 📅 schedule, 🔁 counter, ✅ accept
   - 🏁 submit `away-home` score plus screenshot, ⚠️ dispute
   - 🏠/✈️ request the corresponding force win, 🤝 fair sim, 🆘 commissioner help
4. Commissioners use `/week` for the private management dashboard and official decisions. Normal users get a read-only weekly list.
5. Official results lock their game channels. When every game is final, Advance deletes that week's channels and moves `current_week` forward.

Scheduling reminders stop as soon as a time is confirmed. They run once daily when more than 24 hours remain, every two hours from 24 to 6 hours, and hourly in the final 6 hours. Delivery slots persist across restarts. Missing owners are reported in `commissioner-audit`.

## Owner replacement

Run `/team-release` with a reason. The bot removes the team role, makes the team available, resets only unfinished games, refreshes their matchup cards, and preserves completed games and permanent career history.

## Closing a season

1. Resolve every matchup; `/season-close` refuses while any game is unresolved.
2. Run `/season-close new_season:<name> champion:<member>` and confirm.
3. The bot preserves compact game history, ownership, careers, XP, records, and championship data.
4. It clears members from all team roles, removes temporary season data, preserves configured management destinations and deletes the archived season's matchup channels. The 32 empty roles remain for reuse.
5. If Discord cleanup is partial, fix permissions and run `/season-cleanup`.

## Backups and troubleshooting

Run `/backup` after imports, advancement, and important rulings. Store downloads outside the host.

- Missing commands: invite with `applications.commands`, then restart.
- Role assignment fails: place the bot role above all 32 team roles and rerun `/setup`.
- Missing game channels: confirm the current week's import or have a commissioner run `/week`.
- A reaction does nothing: use the pinned bot card and confirm the member owns one of the two teams.
- A reminder did not post: scheduled/final games do not receive scheduling reminders.
# Madden League Bot — Client Demo Walkthrough

Use one commissioner account and two normal accounts. Prepare one logo per team, one final-score screenshot, and `templates/madden-client-demo.csv`.

## 1. Setup and roles

Run `/setup`:

- league_name: `Jantho Madden Demo`
- game: `Madden 26`
- season: `Demo Season 1`
- timezone: `America/New_York`
- commissioner_role: `@Commissioner`

Show that the bot created all 32 no-permission NFL team roles and that `commissioner-audit` is visible only to commissioners. The bot role must be above the team roles.

## 2. Register owners

Player A runs `/register team:49ers`; Player B runs `/register team:Cowboys`. Show that case does not matter and an invalid/spelled-wrong team produces an embed listing available, pending, and taken teams.

Approve both with `/profile-approve`. Show:

- each owner receives the matching team role;
- each receives the logo-upload DM;
- the audit channel records staff/player actions.

## 3. Import Week 1

Run `/import-schedule`, attach `templates/madden-client-demo.csv`, review, and confirm.

Expected: a Week 1 category and `#49ers-at-cowboys` are created. The channel is publicly readable, but only the two team owners and commissioners can send messages or react. The pinned card shows owners, roles, records, logos, deadline, schedule/result state, and reaction instructions.

## 4. Schedule using reactions

Player A reacts 📅. When prompted, type a future time before the deadline:

`2026-08-01 20:00`

The bot processes/deletes the response and shows the pending time. Player B reacts 🔁 to demonstrate a counter, types another time, then Player A reacts ✅. The pinned card becomes `Scheduled`, shows Discord's localized date/time plus the league timezone, and scheduling reminders stop.

## 5. Submit and approve a result

Player A reacts 🏁. In the channel, type `24-17` and attach the prepared screenshot. The evidence is copied privately to `commissioner-audit`; only the score/status is shown publicly.

Player B reacts ✅ to confirm (or demonstrate ⚠️ to open a dispute). The commissioner runs `/week number:1`, selects the game, chooses **Approve Submitted Score**, and confirms.

Expected: the official result is announced in the game channel, standings/XP update, and the game channel becomes read-only for players. Show `/profile` and `/leaderboard`.

## 6. Commissioner cases

Before finalizing a spare test game, demonstrate:

- 🏠 home force-win request
- ✈️ away force-win request
- 🤝 fair simulation request
- 🆘 commissioner help

Each creates a private audit case and a short public acknowledgement without pinging commissioners in the game channel. The commissioner resolves outcomes through `/week`.

## 7. Advance and next week

Once every Week 1 game is official, run `/week number:1`, choose **Advance to Week 2**, and confirm. The Week 1 channels/category are deleted while results remain in the database.

Upload `templates/madden-client-demo-week-2.csv` only after advancement. This demonstrates that week numbers are not hardcoded and schedules are imported one week at a time.

## 8. Season rollover

Explain that `/season-close` requires every game to be final. When confirmed, it preserves compact game/career/ownership history, clears owners from team roles, preserves configured destinations, deletes temporary matchup channels, and starts a clean season. The empty 32 team roles are reused rather than duplicated.
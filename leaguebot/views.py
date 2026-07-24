from __future__ import annotations

import discord

from .checks import require_commissioner
from .channel_workflow import create_week_matchup_channels
from .db import Database
from .helpers import FINAL_STATUSES, iso_now
from .imports import ImportGame

class ConfirmImportView(discord.ui.View):
    def __init__(
        self, db: Database, games: list[ImportGame], season: str, deadline: str,
        author_id: int,
    ):
        super().__init__(timeout=600)
        self.db = db
        self.games = games
        self.season = season
        self.deadline = deadline
        self.author_id = author_id

    @discord.ui.button(label="Confirm Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the commissioner who previewed this file can confirm it.", ephemeral=True
            )
            return
        settings = await self.db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        created = updated = 0
        conflicts: list[str] = []
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            for game in self.games:
                cursor = await conn.execute(
                    """SELECT * FROM matchups WHERE guild_id=? AND season=?
                       AND week=? AND external_key=?""",
                    (interaction.guild_id, self.season, game.week, game.external_key),
                )
                existing = await cursor.fetchone()
                if existing and existing["status"] in FINAL_STATUSES:
                    conflicts.append(
                        f"Week {game.week} {game.away_team} @ {game.home_team}: official result is locked"
                    )
                    continue
                if existing and existing["result_submitted_by"] is not None:
                    conflicts.append(
                        f"Week {game.week} {game.away_team} @ {game.home_team}: result review is in progress"
                    )
                    continue
                if existing and (
                    existing["away_team"].casefold() != game.away_team.casefold()
                    or existing["home_team"].casefold() != game.home_team.casefold()
                ):
                    conflicts.append(
                        f"Week {game.week} {game.away_team} @ {game.home_team}: teams differ from the posted matchup"
                    )
                    continue

                cursor = await conn.execute(
                    """SELECT user_id FROM profiles WHERE guild_id=? AND approved=1
                       AND lower(team_name)=lower(?)""",
                    (interaction.guild_id, game.away_team),
                )
                away_owner = await cursor.fetchone()
                cursor = await conn.execute(
                    """SELECT user_id FROM profiles WHERE guild_id=? AND approved=1
                       AND lower(team_name)=lower(?)""",
                    (interaction.guild_id, game.home_team),
                )
                home_owner = await cursor.fetchone()
                away_user_id = game.away_user_id or (away_owner["user_id"] if away_owner else None)
                home_user_id = game.home_user_id or (home_owner["user_id"] if home_owner else None)
                now = iso_now()
                if existing:
                    await conn.execute(
                        """UPDATE matchups SET
                           away_user_id=COALESCE(away_user_id,?),
                           home_user_id=COALESCE(home_user_id,?),
                           deadline_at=?,updated_at=? WHERE id=?""",
                        (away_user_id, home_user_id, self.deadline, now, existing["id"]),
                    )
                    updated += 1
                else:
                    await conn.execute(
                        """INSERT INTO matchups
                           (guild_id,season,week,external_key,away_team,home_team,
                            away_user_id,home_user_id,deadline_at,status,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,'waiting',?,?)""",
                        (
                            interaction.guild_id, self.season, game.week, game.external_key,
                            game.away_team, game.home_team, away_user_id, home_user_id,
                            self.deadline, now, now,
                        ),
                    )
                    created += 1
            await conn.commit()
        channels_created = 0
        channel_errors: list[str] = []
        for week in sorted({game.week for game in self.games}):
            made, errors = await create_week_matchup_channels(
                interaction, self.db, season=self.season, week=week
            )
            channels_created += made
            channel_errors.extend(errors)
        await self.db.audit(
            interaction.guild_id, interaction.user.id, "schedule_import",
            details={
                "created": created, "updated": updated, "skipped": len(conflicts),
                "channels_created": channels_created, "channel_errors": len(channel_errors),
                "season": self.season,
            },
        )
        button.disabled = True
        await interaction.edit_original_response(view=self)
        message = (
            f"Import complete: {created} created, {updated} safely updated, "
            f"{len(conflicts)} locked/conflicting row(s) skipped; "
            f"{channels_created} matchup channel(s) created. "
            "Use `/week` to post any matchup threads that do not exist yet."
        )
        if conflicts:
            message += "\n\nSkipped:\n" + "\n".join(f"• {item}" for item in conflicts[:10])
        if channel_errors:
            message += "\n\nChannel errors:\n" + "\n".join(f"• {item}" for item in channel_errors[:5])
        await interaction.followup.send(message[:1900], ephemeral=True)
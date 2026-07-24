from __future__ import annotations

import discord

from .checks import is_commissioner
from .db import Database
from .team_roles import clear_team_role_members
from .season_lifecycle import (
    SeasonClosePreview,
    archive_season,
    resume_season_cleanup,
    rotate_discord_season_space,
    season_close_preview,
)


def season_close_embed(
    preview: SeasonClosePreview,
    *,
    new_season: str,
    champion: discord.Member | str | None,
) -> discord.Embed:
    color = discord.Color.red() if preview.unresolved else discord.Color.gold()
    embed = discord.Embed(
        title=f"Close Madden Season {preview.season}",
        description=(
            f"This will create a fresh operational space for **Season {new_season}**."
        ),
        color=color,
    )
    embed.add_field(
        name="Permanent History Preserved",
        value=(
            f"• {preview.total_games} compact game result(s)\n"
            f"• {preview.participants} participant ownership record(s)\n"
            "• Career wins, losses, XP, levels, win rates, and championships"
        ),
        inline=False,
    )
    embed.add_field(
        name="Operational Data Removed",
        value=(
            "• Old matchup rows, reminders, pending registrations, teams, and audit noise\n"
            "• Old league category, channels, threads, result screenshots, and attachments\n"
            "• Old trade block and current-season transaction workspace"
        ),
        inline=False,
    )
    embed.add_field(
        name="Champion",
        value=(champion.mention if isinstance(champion, discord.Member) else f"<@{champion}>" if champion else "None selected"),
        inline=False,
    )
    if preview.unresolved:
        embed.add_field(
            name=f"Cannot Close · {len(preview.unresolved)} Unresolved",
            value="\n".join(preview.unresolved[:10])[:1024],
            inline=False,
        )
    embed.set_footer(
        text="This is destructive to old Discord channels and requires confirmation."
    )
    return embed


class SeasonCloseConfirmationView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        *,
        guild_id: int,
        season: str,
        new_season: str,
        actor_id: int,
        champion_user_id: int | None,
    ):
        super().__init__(timeout=180)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.new_season = new_season
        self.actor_id = actor_id
        self.champion_user_id = champion_user_id

    @discord.ui.button(
        label="Archive & Start New Season",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation belongs to another Commissioner.", ephemeral=True
            )
            return
        settings = await self.db.settings(self.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only a Commissioner can close a season.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        preview = await season_close_preview(self.db, self.guild_id, self.season)
        if not preview.can_close:
            reason = (
                f"{len(preview.unresolved)} matchup(s) are unresolved."
                if preview.unresolved
                else "This season can no longer be closed from this confirmation."
            )
            await interaction.edit_original_response(content=reason, view=None)
            return
        try:
            result = await archive_season(
                self.db,
                guild_id=self.guild_id,
                season=self.season,
                new_season=self.new_season,
                actor_id=interaction.user.id,
                champion_user_id=self.champion_user_id,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return

        await interaction.edit_original_response(
            content=(
                f"Season {result.season} is safely archived. Creating the fresh "
                f"Season {result.new_season} channels now…"
            ),
            view=None,
        )
        guild = interaction.client.get_guild(self.guild_id)
        cleanup_ok = False
        cleanup_error = "The server is no longer available to the bot."
        role_errors: list[str] = []
        if guild:
            role_errors = await clear_team_role_members(guild, self.db)
            cleanup_ok, cleanup_error = await rotate_discord_season_space(
                guild,
                self.db,
                archived_season=result.season,
                new_season=result.new_season,
            )

        if guild:
            from .ownership import initialize_open_teams
            from .open_teams_ui import refresh_open_teams_panel
            await initialize_open_teams(self.db, self.guild_id, result.new_season)
            await refresh_open_teams_panel(interaction.client, self.db, self.guild_id)

        summary = (
            f"🏁 **Season {result.season} archived**\n"
            f"Games preserved: **{result.games_archived}**\n"
            f"Participants preserved: **{result.participants_preserved}**\n"
            f"New active season: **{result.new_season}**\n"
            f"Discord cleanup: **{'Complete' if cleanup_ok else 'Needs attention'}**\n"
            f"Team roles cleared: **{'Yes' if not role_errors else 'Partial'}**"
        )
        if cleanup_error:
            summary += f"\nCleanup detail: {cleanup_error}"
        await _send_completion_summary(interaction, summary)
        button.disabled = True


class SeasonCleanupRetryView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        *,
        guild_id: int,
        season: str,
        actor_id: int,
    ):
        super().__init__(timeout=180)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.actor_id = actor_id

    @discord.ui.button(
        label="Retry Archived Channel Cleanup",
        style=discord.ButtonStyle.danger,
    )
    async def retry(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation belongs to another Commissioner.", ephemeral=True
            )
            return
        settings = await self.db.settings(self.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only a Commissioner can retry cleanup.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.client.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(
                content="The server is no longer available to the bot.", view=None
            )
            return
        success, error = await resume_season_cleanup(
            guild,
            self.db,
            archived_season=self.season,
        )
        button.disabled = success
        await interaction.edit_original_response(
            content=(
                "Archived Discord cleanup is complete."
                if success
                else f"Cleanup still needs attention: {error}"
            ),
            view=self,
        )

async def _send_completion_summary(
    interaction: discord.Interaction, summary: str
) -> None:
    settings = await interaction.client.db.settings(interaction.guild_id)
    guild = interaction.client.get_guild(interaction.guild_id)
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        try:
            await audit.send(
                embed=discord.Embed(
                    title="New Madden Season Ready",
                    description=summary,
                    color=discord.Color.green(),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    try:
        await interaction.user.send(summary)
    except (discord.Forbidden, discord.HTTPException):
        pass

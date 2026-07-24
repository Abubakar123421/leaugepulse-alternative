from __future__ import annotations

import re

import discord

from .logo_ui import TeamLogoUploadModal


class PersistentTeamLogoButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaguebot:logo:open:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int):
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="Upload Team Logo",
                emoji="🖼️",
                style=discord.ButtonStyle.primary,
                custom_id=f"leaguebot:logo:open:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "PersistentTeamLogoButton":
        return cls(
            guild_id=int(match["guild_id"]),
            user_id=int(match["user_id"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This logo request belongs to another member.", ephemeral=True
            )
            return

        db = interaction.client.db
        profile = await db.fetchone(
            """SELECT team_name, approved FROM profiles
               WHERE guild_id=? AND user_id=?""",
            (self.guild_id, self.user_id),
        )
        if not profile or not profile["approved"]:
            await interaction.response.send_message(
                "You no longer have an approved team in this league.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            TeamLogoUploadModal(
                db,
                self.guild_id,
                self.user_id,
                profile["team_name"],
            )
        )


class PersistentTeamLogoUploadView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=None)
        self.add_item(PersistentTeamLogoButton(guild_id, user_id))

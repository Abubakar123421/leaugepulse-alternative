from __future__ import annotations

import discord


async def is_commissioner(interaction: discord.Interaction, settings: dict) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id = settings.get("commissioner_role_id")
    return bool(role_id and interaction.user.get_role(int(role_id)))


async def require_commissioner(interaction: discord.Interaction, settings: dict) -> bool:
    if await is_commissioner(interaction, settings):
        return True
    if interaction.response.is_done():
        await interaction.followup.send("Only a configured commissioner can do that.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Only a configured commissioner can do that.", ephemeral=True
        )
    return False


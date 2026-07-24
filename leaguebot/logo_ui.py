from __future__ import annotations

import discord

from .db import Database

ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_LOGO_BYTES = 8 * 1024 * 1024


class TeamLogoUploadModal(discord.ui.Modal):
    def __init__(self, db: Database, guild_id: int, user_id: int, team_name: str):
        super().__init__(
            title="Upload Your Team Logo",
            timeout=600,
            custom_id=f"leaguebot:logo:modal:{guild_id}:{user_id}",
        )
        self.db = db
        self.guild_id = guild_id
        self.user_id = user_id
        self.team_name = team_name
        self.upload = discord.ui.FileUpload(
            custom_id="leaguebot:logo:file",
            required=True,
            min_values=1,
            max_values=1,
        )
        self.add_item(
            discord.ui.Label(
                text=f"{team_name} logo",
                description="Upload one PNG, JPG, or WebP image (maximum 8 MB).",
                component=self.upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This logo request belongs to another member.", ephemeral=True
            )
            return

        profile = await self.db.fetchone(
            """SELECT team_name, approved FROM profiles
               WHERE guild_id=? AND user_id=?""",
            (self.guild_id, self.user_id),
        )
        if not profile or not profile["approved"] or profile["team_name"] != self.team_name:
            await interaction.response.send_message(
                "Your approved team assignment has changed. Ask a commissioner to review it.",
                ephemeral=True,
            )
            return

        attachment = self.upload.values[0]
        if attachment.content_type not in ALLOWED_LOGO_TYPES:
            await interaction.response.send_message(
                "Please upload a PNG, JPG, or WebP image.", ephemeral=True
            )
            return
        if attachment.size > MAX_LOGO_BYTES:
            await interaction.response.send_message(
                "That image is larger than 8 MB. Please upload a smaller version.",
                ephemeral=True,
            )
            return

        uploaded_file = await attachment.to_file(
            filename=f"{self.team_name.replace(' ', '-')}-logo.{_extension(attachment)}"
        )
        await interaction.response.send_message(
            f"Uploading the **{self.team_name}** logo…",
            file=uploaded_file,
        )
        response_message = await interaction.original_response()
        if not response_message.attachments:
            await interaction.edit_original_response(
                content="Discord accepted the upload, but no image URL was returned. Please try again."
            )
            return

        logo_url = response_message.attachments[0].url
        settings = await self.db.settings(self.guild_id)
        await self.db.execute(
            """INSERT INTO teams (guild_id,season,name,logo_url) VALUES (?,?,?,?)
               ON CONFLICT(guild_id,season,name)
               DO UPDATE SET logo_url=excluded.logo_url""",
            (self.guild_id, settings["season"], self.team_name, logo_url),
        )
        await self.db.audit(
            self.guild_id,
            self.user_id,
            "team_logo_uploaded",
            target_type="team",
            target_id=self.team_name,
        )

        confirmation = discord.Embed(
            title="Team Logo Saved",
            description=(
                f"Your **{self.team_name}** logo is ready. It will appear in future "
                "matchup posts for this league."
            ),
            color=discord.Color.green(),
        )
        confirmation.set_thumbnail(url=logo_url)
        await interaction.edit_original_response(content=None, embed=confirmation)

        guild = interaction.client.get_guild(self.guild_id)
        audit_channel = (
            guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
        )
        if isinstance(audit_channel, discord.TextChannel):
            await audit_channel.send(
                embed=discord.Embed(
                    title="Team Logo Uploaded",
                    description=f"<@{self.user_id}> uploaded the **{self.team_name}** logo.",
                    color=discord.Color.blurple(),
                ).set_thumbnail(url=logo_url)
            )


class TeamLogoUploadView(discord.ui.View):
    def __init__(self, db: Database, guild_id: int, user_id: int, team_name: str):
        super().__init__(timeout=7 * 24 * 60 * 60)
        self.db = db
        self.guild_id = guild_id
        self.user_id = user_id
        self.team_name = team_name

    @discord.ui.button(
        label="Upload Team Logo",
        emoji="🖼️",
        style=discord.ButtonStyle.primary,
        custom_id="leaguebot:logo:open",
    )
    async def upload_logo(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This logo request belongs to another member.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            TeamLogoUploadModal(
                self.db, self.guild_id, self.user_id, self.team_name
            )
        )


def approval_embed(
    *, league_name: str, game: str, season: str, team_name: str
) -> discord.Embed:
    embed = discord.Embed(
        title="Welcome to the League!",
        description=(
            f"Your request to control **{team_name}** has been approved.\n\n"
            "You are now the official team owner for this Discord league. "
            "Use the button below to personalize your matchup posts with a team logo."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="League", value=league_name)
    embed.add_field(name="Game", value=game)
    embed.add_field(name="Season", value=season)
    embed.add_field(name="Your Team", value=team_name)
    embed.add_field(
        name="Next Step",
        value="Upload one PNG, JPG, or WebP logo. You can skip this and continue later.",
        inline=False,
    )
    embed.set_footer(text="Your commissioner can replace the logo if assistance is needed.")
    return embed


def _extension(attachment: discord.Attachment) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }[attachment.content_type]

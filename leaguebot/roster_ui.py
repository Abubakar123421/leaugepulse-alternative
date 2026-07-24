from __future__ import annotations

import discord

from .checks import is_commissioner
from .channel_workflow import refresh_matchup_message
from .db import Database
from .helpers import FINAL_STATUSES, iso_now
from .team_roles import remove_team_role


class TeamReleaseConfirmationView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        *,
        guild_id: int,
        season: str,
        user_id: int,
        team_name: str,
        reason: str,
        actor_id: int,
    ):
        super().__init__(timeout=180)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.user_id = user_id
        self.team_name = team_name
        self.reason = reason
        self.actor_id = actor_id

    @discord.ui.button(
        label="Confirm Team Release",
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
                "Your Commissioner access was removed.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await self.db.fetchone(
            """SELECT * FROM profiles
               WHERE guild_id=? AND user_id=? AND approved=1""",
            (self.guild_id, self.user_id),
        )
        if not profile or profile["team_name"].casefold() != self.team_name.casefold():
            await interaction.edit_original_response(
                content="This owner/team assignment has already changed.", view=None
            )
            return
        pending_result = await self.db.fetchone(
            """SELECT id FROM matchups
               WHERE guild_id=? AND season=?
               AND (away_user_id=? OR home_user_id=?)
               AND status IN ('result_pending','issue_reported') LIMIT 1""",
            (self.guild_id, self.season, self.user_id, self.user_id),
        )
        if pending_result:
            await interaction.edit_original_response(
                content=(
                    f"Resolve matchup #{pending_result['id']} before releasing this owner; "
                    "it has a result or active dispute."
                ),
                view=None,
            )
            return

        async with self.db.connect() as conn:
            await conn.execute(
                """UPDATE matchups SET away_user_id=NULL,status='waiting',
                   scheduled_at=NULL,schedule_previous_at=NULL,proposed_by=NULL,
                   proposed_at=NULL,updated_at=?
                   WHERE guild_id=? AND season=? AND away_user_id=?
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (iso_now(), self.guild_id, self.season, self.user_id),
            )
            await conn.execute(
                """UPDATE matchups SET home_user_id=NULL,status='waiting',
                   scheduled_at=NULL,schedule_previous_at=NULL,proposed_by=NULL,
                   proposed_at=NULL,updated_at=?
                   WHERE guild_id=? AND season=? AND home_user_id=?
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (iso_now(), self.guild_id, self.season, self.user_id),
            )
            await conn.execute(
                "DELETE FROM profiles WHERE guild_id=? AND user_id=?",
                (self.guild_id, self.user_id),
            )
            await conn.execute(
                """INSERT INTO open_rosters
                   (guild_id,season,team_name,notes,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(guild_id,season,team_name)
                   DO UPDATE SET notes=excluded.notes,updated_at=excluded.updated_at""",
                (
                    self.guild_id,
                    self.season,
                    self.team_name,
                    f"Owner released: {self.reason}",
                    iso_now(),
                ),
            )
            await conn.commit()
        current_member = interaction.guild.get_member(self.user_id) if interaction.guild else None
        if current_member:
            try:
                await remove_team_role(self.db, current_member, self.team_name)
            except (discord.Forbidden, discord.HTTPException):
                pass
        affected = await self.db.fetchall(
            """SELECT id FROM matchups WHERE guild_id=? AND season=?
               AND (lower(away_team)=lower(?) OR lower(home_team)=lower(?))
               AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (self.guild_id, self.season, self.team_name, self.team_name),
        )
        for matchup in affected:
            await refresh_matchup_message(interaction.client, self.db, matchup["id"])
        await self.db.audit(
            self.guild_id,
            interaction.user.id,
            "team_owner_released",
            target_type="user",
            target_id=str(self.user_id),
            details={"team": self.team_name, "reason": self.reason},
        )
        from .open_teams_ui import refresh_open_team_card
        await refresh_open_team_card(interaction.client, self.db, self.guild_id, self.team_name)
        await _notify_release(
            interaction,
            settings,
            user_id=self.user_id,
            team_name=self.team_name,
            reason=self.reason,
        )
        button.disabled = True
        await interaction.edit_original_response(
            content=(
                f"Released <@{self.user_id}> from **{self.team_name}**. "
                "Their history remains permanent, unfinished games were reset for a "
                "replacement, and the team is available again."
            ),
            view=self,
        )


async def _notify_release(
    interaction: discord.Interaction,
    settings: dict,
    *,
    user_id: int,
    team_name: str,
    reason: str,
) -> None:
    guild = interaction.client.get_guild(interaction.guild_id)
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        try:
            await audit.send(
                embed=discord.Embed(
                    title="Team Owner Released",
                    description=(
                        f"**Owner:** <@{user_id}>\n"
                        f"**Team:** {team_name}\n"
                        f"**Reason:** {reason}\n"
                        f"**Commissioner:** {interaction.user.mention}"
                    ),
                    color=discord.Color.gold(),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    user = interaction.client.get_user(user_id)
    if user is None:
        try:
            user = await interaction.client.fetch_user(user_id)
        except discord.HTTPException:
            user = None
    if user:
        try:
            await user.send(
                f"You were released from **{team_name}** for the current Madden season. "
                f"Reason: {reason}. Your career and season history were preserved."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
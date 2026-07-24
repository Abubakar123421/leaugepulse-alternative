from __future__ import annotations

import re
from datetime import datetime

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import FINAL_STATUSES, iso_now, parse_user_datetime, utcnow


class ScheduleProposalModal(discord.ui.Modal, title="Propose Game Time"):
    proposed = discord.ui.TextInput(
        label="Proposed date and time",
        placeholder="2026-08-15 20:30",
        max_length=40,
    )

    def __init__(self, db: Database, matchup_id: int, timezone: str):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:schedule:modal:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id
        self.timezone = timezone

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            scheduled = parse_user_datetime(str(self.proposed), self.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.edit_original_response(
                content="This matchup no longer exists."
            )
            return

        if matchup["status"] in FINAL_STATUSES:
            await interaction.edit_original_response(
                content="This matchup is already final."
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if (
            interaction.user.id not in (matchup["away_user_id"], matchup["home_user_id"])
            and not await is_commissioner(interaction, settings)
        ):
            await interaction.edit_original_response(
                content="Only the assigned owners or a Commissioner can propose a time."
            )
            return
        if scheduled <= utcnow():
            await interaction.edit_original_response(
                content="Proposed game time must be in the future."
            )
            return
        if matchup["deadline_at"]:
            deadline = datetime.fromisoformat(matchup["deadline_at"])
            if scheduled > deadline:
                await interaction.edit_original_response(
                    content=(
                        "That time is after the advance deadline. Use **Availability Help** "
                        "to request an extension first."
                    )
                )
                return
        previous = matchup["schedule_previous_at"]
        if matchup["status"] == "scheduled":
            previous = matchup["scheduled_at"]
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET proposed_by=?, proposed_at=?, scheduled_at=?,
                   schedule_previous_at=?,
                   schedule_proposal_version=schedule_proposal_version+1,
                   status='schedule_pending', updated_at=?
                   WHERE id=? AND status NOT IN ('complete','force_home','force_away','fair_sim')
                   RETURNING schedule_proposal_version""",
                (
                    interaction.user.id,
                    iso_now(),
                    scheduled.isoformat(),
                    previous,
                    iso_now(),
                    self.matchup_id,
                ),
            )
            version_row = await cursor.fetchone()
            await conn.commit()
        if not version_row:
            await interaction.edit_original_response(
                content="This matchup was finalized while you were submitting the proposal."
            )
            return
        proposal_version = version_row[0]
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "schedule_proposed",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={"scheduled_at": scheduled.isoformat(), "version": proposal_version},
        )

        notified = await _notify_opponents(
            interaction, matchup, scheduled, interaction.user.id, proposal_version
        )
        await _post_schedule_audit(
            interaction.client,
            matchup,
            title="Schedule Proposed",
            description=(
                f"<@{interaction.user.id}> proposed <t:{int(scheduled.timestamp())}:F> "
                f"for **{matchup['away_team']} @ {matchup['home_team']}**."
            ),
            color=discord.Color.gold(),
        )

        message = (
            f"📅 Your proposal for <t:{int(scheduled.timestamp())}:F> was saved. "
            "Your opponent received a private Accept / Counter / Decline request."
            if notified
            else (
                f"📅 Your proposal for <t:{int(scheduled.timestamp())}:F> was saved, "
                "but I could not DM an assigned opponent. A commissioner can review it "
                "in the audit channel."
            )
        )
        await interaction.edit_original_response(content=message)


class ScheduleDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"leaguebot:schedule:(?P<action>accept|decline|counter):"
        r"(?P<matchup_id>\d+)(?::(?P<version>\d+))?"
    ),
):
    STYLES = {
        "accept": discord.ButtonStyle.success,
        "decline": discord.ButtonStyle.danger,
        "counter": discord.ButtonStyle.primary,
    }

    def __init__(self, action: str, matchup_id: int, version: int | None = None):
        self.action = action
        self.matchup_id = matchup_id
        self.version = version
        super().__init__(
            discord.ui.Button(
                label=action.title() if action != "counter" else "Counter Proposal",
                style=self.STYLES[action],
                custom_id=(
                    f"leaguebot:schedule:{action}:{matchup_id}"
                    + (f":{version}" if version is not None else "")
                ),
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "ScheduleDecisionButton":
        version = int(match["version"]) if match["version"] else None
        return cls(match["action"], int(match["matchup_id"]), version)

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        matchup = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.response.send_message(
                "This matchup no longer exists.", ephemeral=True
            )
            return
        if (
            (self.version is None and matchup["schedule_proposal_version"] != 0)
            or (self.version is not None and self.version != matchup["schedule_proposal_version"])
        ):
            await interaction.response.send_message(
                "This is an older scheduling proposal. Use the newest DM instead.",
                ephemeral=True,
            )
            return
        if matchup["status"] != "schedule_pending" or not matchup["scheduled_at"]:
            await interaction.response.send_message(
                "This proposal is no longer awaiting a decision.", ephemeral=True
            )
            return
        if interaction.user.id == matchup["proposed_by"]:
            await interaction.response.send_message(
                "You cannot approve your own proposal.", ephemeral=True
            )
            return
        if interaction.user.id not in (
            matchup["away_user_id"],
            matchup["home_user_id"],
        ):
            await interaction.response.send_message(
                "Only the opposing team owner can decide this proposal.", ephemeral=True
            )
            return

        settings = await db.settings(matchup["guild_id"])
        scheduled = datetime.fromisoformat(matchup["scheduled_at"])
        if self.action == "counter":
            await interaction.response.send_modal(
                ScheduleProposalModal(
                    db, self.matchup_id, settings["timezone"]
                )
            )
            return

        await interaction.response.defer()

        if self.action == "accept":
            new_status = "scheduled"
            scheduled_value = matchup["scheduled_at"]
        else:
            scheduled_value = matchup["schedule_previous_at"]
            new_status = "scheduled" if scheduled_value else "waiting"
        async with db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET status=?,scheduled_at=?,schedule_previous_at=NULL,
                   updated_at=? WHERE id=? AND status='schedule_pending'
                   AND schedule_proposal_version=?""",
                (
                    new_status,
                    scheduled_value,
                    iso_now(),
                    self.matchup_id,
                    matchup["schedule_proposal_version"],
                ),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            await interaction.edit_original_response(
                content="Decision lost because the matchup changed. Open the latest request."
            )
            return
        await db.audit(
            matchup["guild_id"],
            interaction.user.id,
            f"schedule_{self.action}ed",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={"scheduled_at": matchup["scheduled_at"]},
        )

        if self.action == "accept":
            title = "Game Time Confirmed"
            description = (
                f"<@{interaction.user.id}> accepted <t:{int(scheduled.timestamp())}:F> "
                f"for **{matchup['away_team']} @ {matchup['home_team']}**."
            )
            color = discord.Color.green()
            dm_text = (
                f"✅ Your opponent accepted the game time "
                f"<t:{int(scheduled.timestamp())}:F>."
            )
        else:
            title = "Schedule Proposal Declined"
            description = (
                f"<@{interaction.user.id}> declined <t:{int(scheduled.timestamp())}:F> "
                f"for **{matchup['away_team']} @ {matchup['home_team']}**."
            )
            color = discord.Color.red()
            dm_text = (
                f"❌ Your opponent declined the proposed game time "
                f"<t:{int(scheduled.timestamp())}:F>."
            )

        await _post_schedule_audit(
            interaction.client,
            matchup,
            title=title,
            description=description,
            color=color,
        )
        await _dm_user(interaction.client, matchup["proposed_by"], dm_text)
        await interaction.edit_original_response(
            content=dm_text,
            embed=None,
            view=None,
        )


class ScheduleDecisionView(discord.ui.View):
    def __init__(self, matchup_id: int, version: int | None = None):
        super().__init__(timeout=None)
        self.add_item(ScheduleDecisionButton("accept", matchup_id, version))
        self.add_item(ScheduleDecisionButton("counter", matchup_id, version))
        self.add_item(ScheduleDecisionButton("decline", matchup_id, version))


async def _notify_opponents(
    interaction: discord.Interaction,
    matchup,
    scheduled: datetime,
    proposer_id: int,
    version: int,
) -> bool:
    opponent_ids = {
        value
        for value in (matchup["away_user_id"], matchup["home_user_id"])
        if value and value != proposer_id
    }
    if not opponent_ids:
        return False

    embed = discord.Embed(
        title="New Game-Time Proposal",
        description=(
            f"<@{proposer_id}> proposed <t:{int(scheduled.timestamp())}:F> "
            f"for **{matchup['away_team']} @ {matchup['home_team']}**."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Your Decision",
        value="Accept this time, decline it, or send a counter proposal.",
        inline=False,
    )
    sent = False
    for opponent_id in opponent_ids:
        user = interaction.client.get_user(opponent_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(opponent_id)
            except discord.HTTPException:
                continue
        try:
            await user.send(
                embed=embed,
                view=ScheduleDecisionView(matchup["id"], version),
            )
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            continue
    return sent


async def _post_schedule_audit(
    client: discord.Client,
    matchup,
    *,
    title: str,
    description: str,
    color: discord.Color,
) -> None:
    settings = await client.db.settings(matchup["guild_id"])
    guild = client.get_guild(matchup["guild_id"])
    channel = (
        guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    )
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(
                embed=discord.Embed(
                    title=title,
                    description=description,
                    color=color,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _dm_user(client: discord.Client, user_id: int | None, message: str) -> None:
    if not user_id:
        return
    user = client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except discord.HTTPException:
            return
    try:
        await user.send(message)
    except (discord.Forbidden, discord.HTTPException):
        pass

from __future__ import annotations

from datetime import datetime, timedelta

import discord

from .availability_ui import create_or_update_case

from .db import Database
from .helpers import iso_now, utcnow


class CommissionerRequestModal(discord.ui.Modal, title="Request Commissioner Help"):
    reason = discord.ui.TextInput(
        label="What do you need help with?",
        placeholder="Scheduling conflict, opponent unresponsive, result question…",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1000,
    )

    def __init__(self, db: Database, matchup_id: int):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:commissioner:request:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.edit_original_response(
                content="This matchup no longer exists."
            )
            return
        if matchup["commissioner_pinged_at"]:
            last = datetime.fromisoformat(matchup["commissioner_pinged_at"])
            remaining = timedelta(hours=2) - (utcnow() - last)
            if remaining.total_seconds() > 0:
                minutes = max(1, int(remaining.total_seconds() // 60))
                await interaction.edit_original_response(
                    content=(
                        "A commissioner was already requested recently. "
                        f"Try again in approximately {minutes} minute(s)."
                    )
                )
                return

        settings = await self.db.settings(matchup["guild_id"])
        channel = _audit_channel(interaction.client, matchup["guild_id"], settings)
        if channel is None:
            await interaction.edit_original_response(
                content="The commissioner audit channel is not configured. Ask an admin to rerun `/setup`."
            )
            return

        await self.db.execute(
            "UPDATE matchups SET commissioner_pinged_at=?, updated_at=? WHERE id=?",
            (iso_now(), iso_now(), self.matchup_id),
        )
        case_id = await create_or_update_case(
            self.db,
            matchup=matchup,
            opened_by=interaction.user.id,
            kind="help",
            reason=str(self.reason),
        )
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "commissioner_requested",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={"reason": str(self.reason), "case_id": case_id},
        )
        await _send_alert(
            channel,
            settings,
            title="Commissioner Assistance Requested",
            description=(
                f"**Requested by:** <@{interaction.user.id}>\n"
                f"**Matchup:** #{matchup['id']} · {matchup['away_team']} @ {matchup['home_team']}\n"
                f"**Matchup channel:** {_thread_link(matchup)}\n"
                f"**Reason:**\n{self.reason}"
            ),
            color=discord.Color.gold(),
        )
        await interaction.edit_original_response(
            content=(
                "Your request was sent privately to the Commissioner team. "
                "It was not posted in the public matchup channel."
            )
        )


class IssueReportModal(discord.ui.Modal, title="Report Matchup Issue"):
    details = discord.ui.TextInput(
        label="Describe the issue",
        placeholder="Explain what happened and what assistance you need.",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1000,
    )

    def __init__(self, db: Database, matchup_id: int):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:issue:report:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.edit_original_response(
                content="This matchup no longer exists."
            )
            return

        settings = await self.db.settings(matchup["guild_id"])
        channel = _audit_channel(interaction.client, matchup["guild_id"], settings)
        if channel is None:
            await interaction.edit_original_response(
                content="The commissioner audit channel is not configured. Ask an admin to rerun `/setup`."
            )
            return

        await self.db.execute(
            """UPDATE matchups SET issue_text=?,updated_at=? WHERE id=?""",
            (str(self.details), iso_now(), self.matchup_id),
        )
        case_id = await create_or_update_case(
            self.db,
            matchup=matchup,
            opened_by=interaction.user.id,
            kind="issue",
            reason=str(self.details),
        )
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "issue_reported",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={"issue": str(self.details), "case_id": case_id},
        )
        await _send_alert(
            channel,
            settings,
            title="Matchup Issue Reported",
            description=(
                f"**Reported by:** <@{interaction.user.id}>\n"
                f"**Matchup:** #{matchup['id']} · {matchup['away_team']} @ {matchup['home_team']}\n"
                f"**Matchup channel:** {_thread_link(matchup)}\n"
                f"**Issue details:**\n{self.details}"
            ),
            color=discord.Color.red(),
        )
        await interaction.edit_original_response(
            content=(
                "Your issue was recorded and sent privately to the Commissioner team. "
                "The matchup is now flagged for staff review."
            )
        )


def _audit_channel(
    client: discord.Client, guild_id: int, settings: dict
) -> discord.TextChannel | None:
    guild = client.get_guild(guild_id)
    channel = (
        guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    )
    return channel if isinstance(channel, discord.TextChannel) else None


async def _send_alert(
    channel: discord.TextChannel,
    settings: dict,
    *,
    title: str,
    description: str,
    color: discord.Color,
) -> None:
    mention = (
        f"<@&{settings['commissioner_role_id']}>"
        if settings.get("commissioner_role_id")
        else "Commissioners"
    )
    await channel.send(
        mention,
        embed=discord.Embed(
            title=title,
            description=description,
            color=color,
        ),
        allowed_mentions=discord.AllowedMentions(
            roles=True, users=False, everyone=False
        ),
    )


def _thread_link(matchup) -> str:
    return f"<#{matchup['channel_id']}>" if matchup["channel_id"] else "Not created"

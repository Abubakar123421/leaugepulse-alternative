from __future__ import annotations

import asyncio

import discord

from .availability_ui import CaseResolutionView, case_embed, latest_open_case

from .checks import is_commissioner
from .channel_workflow import lock_and_delete_week_channels, refresh_matchup_message
from .db import Database
from .helpers import FINAL_STATUSES, iso_now, status_label
from .progression import record_matchup_progress, reverse_matchup_progress
from .team_emojis import team_label
from .result_ui import CommissionerResultConfirmationView


class MatchupSelect(discord.ui.Select):
    def __init__(self, db: Database, rows, actor_id: int):
        self.db = db
        self.actor_id = actor_id
        options = [
            discord.SelectOption(
                label=f"W{row['week']} · {row['away_team']} @ {row['home_team']}"[:100],
                value=str(row["id"]),
                description=status_label(row["status"], row["deadline_at"])[:100],
            )
            for row in rows[:25]
        ]
        super().__init__(
            placeholder="Select a matchup to manage…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This dashboard belongs to another Commissioner.", ephemeral=True
            )
            return
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=? AND guild_id=?",
            (int(self.values[0]), interaction.guild_id),
        )
        if not matchup:
            await interaction.response.send_message(
                "That matchup no longer exists.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=commissioner_matchup_embed(matchup),
            view=CommissionerMatchupActionsView(
                self.db, matchup, interaction.user.id
            ),
            ephemeral=True,
        )


class CommissionerWeekDashboardView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        rows,
        *,
        guild_id: int,
        season: str,
        week: int,
        actor_id: int,
        can_advance: bool = True,
    ):
        super().__init__(timeout=900)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.week = week
        self.actor_id = actor_id
        self.add_item(MatchupSelect(db, rows, actor_id))
        if not can_advance:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "Advance to Next Week":
                    child.disabled = True
                    child.label = "Archived Week"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message(
            "This dashboard belongs to another Commissioner.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Advance to Next Week",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def advance(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        settings = await self.db.settings(self.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only a Commissioner can advance the week.", ephemeral=True
            )
            return
        unresolved = await self.db.fetchone(
            """SELECT COUNT(*) AS total FROM matchups
               WHERE guild_id=? AND season=? AND week=?
               AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (self.guild_id, self.season, self.week),
        )

        await interaction.response.send_message(
            f"Advance from **Week {self.week}** with **{unresolved['total']} unresolved** game(s)? They remain saved for later decisions.",
            view=AdvanceWeekConfirmationView(
                self.db,
                self.guild_id,
                self.season,
                self.week,
                interaction.user.id,
            ),
            ephemeral=True,
        )


class CommissionerMatchupActionsView(discord.ui.View):
    def __init__(self, db: Database, matchup, actor_id: int):
        super().__init__(timeout=600)
        self.db = db
        self.matchup_id = matchup["id"]
        self.actor_id = actor_id
        self.force_away.label = f"Force Win: {matchup['away_team']}"[:80]
        self.force_home.label = f"Force Win: {matchup['home_team']}"[:80]
        self.reopen_final.disabled = matchup["status"] not in FINAL_STATUSES

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This dashboard belongs to another Commissioner.", ephemeral=True
            )
            return False
        matchup = await self.db.fetchone(
            "SELECT guild_id FROM matchups WHERE id=?", (self.matchup_id,)
        )
        settings = await self.db.settings(matchup["guild_id"]) if matchup else {}
        if matchup and await is_commissioner(interaction, settings):
            return True
        await interaction.response.send_message(
            "Only a Commissioner can manage this matchup.", ephemeral=True
        )
        return False

    async def matchup(self):
        return await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )

    @discord.ui.button(
        label="Approve Submitted Score",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def approve(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        matchup = await self.matchup()
        if (
            not matchup
            or matchup["status"] not in ("result_pending", "issue_reported")
            or not matchup["result_submitted_by"]
            or not matchup["result_evidence_url"]
        ):
            await interaction.response.send_message(
                "There is no complete score-and-screenshot submission to approve.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Review the screenshot in `commissioner-audit`, then confirm this approval.",
            view=CommissionerResultConfirmationView(
                self.db, self.matchup_id, "approve", interaction.user.id,
                matchup["result_submission_version"]
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Send Reminder",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def reminder(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.matchup()
        if not matchup or matchup["status"] in FINAL_STATUSES:
            await interaction.edit_original_response(
                content="This matchup is already final or no longer exists."
            )
            return
        sent = 0
        for user_id in {
            matchup["away_user_id"],
            matchup["home_user_id"],
        } - {None}:
            user = interaction.client.get_user(user_id)
            if user is None:
                try:
                    user = await interaction.client.fetch_user(user_id)
                except discord.HTTPException:
                    continue
            try:
                await user.send(
                    f"⏰ Commissioner reminder for **{matchup['away_team']} @ "
                    f"{matchup['home_team']}**. Please coordinate and complete your game."
                )
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "manual_matchup_reminder",
            target_type="matchup",
            target_id=str(matchup["id"]),
            details={"delivered": sent},
        )
        await send_audit_notice(
            interaction,
            matchup,
            "Manual Reminder Sent",
            f"{interaction.user.mention} sent a matchup reminder. Delivered to **{sent}** owner(s).",
        )
        await interaction.edit_original_response(
            content=f"Reminder delivered privately to **{sent}** team owner(s)."
        )

    @discord.ui.button(
        label="Force Win: Away",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def force_away(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._confirm_outcome(interaction, "force_away")

    @discord.ui.button(
        label="Force Win: Home",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def force_home(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._confirm_outcome(interaction, "force_home")

    @discord.ui.button(
        label="Fair Sim",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def fair_sim(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._confirm_outcome(interaction, "fair_sim")

    @discord.ui.button(
        label="Review Issue",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def review_issue(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        matchup = await self.matchup()
        case = await latest_open_case(self.db, self.matchup_id)
        if case and matchup:
            await interaction.response.send_message(
                embed=case_embed(case, matchup),
                view=CaseResolutionView(self.db, case, interaction.user.id),
                ephemeral=True,
            )
            return
        issue = matchup["issue_text"] if matchup else None
        embed = discord.Embed(
            title="Matchup Issue Review",
            description=issue or "No issue has been reported for this matchup.",
            color=discord.Color.red() if issue else discord.Color.light_grey(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Reopen Final",
        style=discord.ButtonStyle.danger,
        row=3,
    )
    async def reopen_final(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        matchup = await self.matchup()
        if not matchup or matchup["status"] not in FINAL_STATUSES:
            await interaction.response.send_message(
                "This matchup is not final.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "This reverses standings and XP, removes the official result, and reopens the game. Confirm?",
            view=ReopenFinalConfirmationView(
                self.db, self.matchup_id, interaction.user.id
            ),
            ephemeral=True,
        )
    async def _confirm_outcome(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        matchup = await self.matchup()
        if not matchup or matchup["status"] in FINAL_STATUSES:
            await interaction.response.send_message(
                "This matchup is already final or no longer exists.", ephemeral=True
            )
            return
        labels = {
            "force_away": f"award a force win to {matchup['away_team']}",
            "force_home": f"award a force win to {matchup['home_team']}",
            "fair_sim": "mark this matchup for a neutral fair simulation",
        }
        await interaction.response.send_message(
            f"Confirm: **{labels[action]}**?",
            view=OutcomeConfirmationView(
                self.db, self.matchup_id, action, interaction.user.id
            ),
            ephemeral=True,
        )


class OutcomeConfirmationView(discord.ui.View):
    def __init__(self, db: Database, matchup_id: int, action: str, actor_id: int):
        super().__init__(timeout=120)
        self.db = db
        self.matchup_id = matchup_id
        self.action = action
        self.actor_id = actor_id

    @discord.ui.button(label="Confirm Decision", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation belongs to another Commissioner.", ephemeral=True
            )
            return
        await interaction.response.defer()
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup or matchup["status"] in FINAL_STATUSES:
            await interaction.edit_original_response(
                content="This matchup has already been finalized.", view=None
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.edit_original_response(
                content="Your Commissioner access was removed.", view=None
            )
            return
        applied = await apply_commissioner_outcome(
            self.db, matchup, self.action, interaction.user.id
        )
        if not applied:
            await interaction.edit_original_response(
                content="Another Commissioner already handled this matchup.", view=None
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            self.action,
            target_type="matchup",
            target_id=str(matchup["id"]),
        )
        await send_audit_notice(
            interaction,
            matchup,
            "Commissioner Matchup Decision",
            outcome_description(matchup, self.action),
        )
        await publish_outcome(interaction.client, matchup, self.action)
        updated = await self.db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup["id"],))
        from .weekly_content import publish_weekly_recap
        await publish_weekly_recap(
            interaction.client, self.db, updated["guild_id"], updated["season"],
            updated["week"], create=False, regenerate_ai=True,
        )
        button.disabled = True
        await interaction.edit_original_response(
            content=f"Confirmed: **{outcome_description(matchup, self.action)}**",
            view=self,
        )


class ReopenFinalConfirmationView(discord.ui.View):
    def __init__(self, db: Database, matchup_id: int, actor_id: int):
        super().__init__(timeout=120)
        self.db = db
        self.matchup_id = matchup_id
        self.actor_id = actor_id

    @discord.ui.button(label="Confirm Reopen Final", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation belongs to another Commissioner.", ephemeral=True
            )
            return
        await interaction.response.defer()
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup or matchup["status"] not in FINAL_STATUSES:
            await interaction.edit_original_response(
                content="This matchup is no longer final.", view=None
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.edit_original_response(
                content="Your Commissioner access was removed.", view=None
            )
            return
        if not await reopen_final_matchup(
            self.db, matchup, interaction.user.id
        ):
            await interaction.edit_original_response(
                content="Another Commissioner changed this matchup first.", view=None
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "official_result_reopened",
            target_type="matchup",
            target_id=str(matchup["id"]),
            details={"previous_status": matchup["status"]},
        )
        await publish_reopened_matchup(interaction.client, matchup)
        for user_id in {matchup["away_user_id"], matchup["home_user_id"]} - {None}:
            user = interaction.client.get_user(user_id)
            if user:
                try:
                    await user.send(
                        f"Commissioner reopened **{matchup['away_team']} @ "
                        f"{matchup['home_team']}**. The previous result and progression "
                        "were reversed."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        button.disabled = True
        await interaction.edit_original_response(
            content="Official result reversed and matchup reopened safely.", view=self
        )

class AdvanceWeekConfirmationView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        guild_id: int,
        season: str,
        week: int,
        actor_id: int,
    ):
        super().__init__(timeout=120)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.week = week
        self.actor_id = actor_id

    @discord.ui.button(label="Confirm Week Advance", style=discord.ButtonStyle.danger)
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
        await interaction.response.defer(ephemeral=True)
        service = getattr(interaction.client, "week_rollovers", None)
        if service is None:
            await interaction.edit_original_response(
                content="The week rollover service is unavailable. Restart the bot and try again.",
                view=self,
            )
            return
        ok, detail = await service.rollover(
            self.guild_id, expected_week=self.week, actor_id=interaction.user.id
        )
        if ok:
            button.disabled = True
        await interaction.edit_original_response(content=detail, view=self)


async def apply_commissioner_outcome(
    db: Database, matchup, action: str, actor_id: int
) -> bool:
    now = iso_now()
    async with db.connect() as conn:
        cursor = await conn.execute(
            """UPDATE matchups SET status=?, result_reviewed_by=?,
               result_reviewed_at=?, result_review_note=?, updated_at=?
               WHERE id=? AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (action, actor_id, now, action.replace("_", " ").title(), now, matchup["id"]),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return False
        if action in ("force_away", "force_home"):
            for team_name in (matchup["away_team"], matchup["home_team"]):
                await conn.execute(
                    """INSERT INTO teams (guild_id,season,name) VALUES (?,?,?)
                       ON CONFLICT(guild_id,season,name) DO NOTHING""",
                    (matchup["guild_id"], matchup["season"], team_name),
                )
            winner = (
                matchup["away_team"] if action == "force_away" else matchup["home_team"]
            )
            loser = (
                matchup["home_team"] if action == "force_away" else matchup["away_team"]
            )
            await conn.execute(
                """UPDATE teams SET wins=wins+1
                   WHERE guild_id=? AND season=? AND lower(name)=lower(?)""",
                (matchup["guild_id"], matchup["season"], winner),
            )
            await conn.execute(
                """UPDATE teams SET losses=losses+1
                   WHERE guild_id=? AND season=? AND lower(name)=lower(?)""",
                (matchup["guild_id"], matchup["season"], loser),
            )
        await record_matchup_progress(conn, matchup, action)
        await conn.execute(
            """UPDATE matchup_cases SET status='resolved',
               resolution='Matchup finalized by Commissioner',resolved_by=?,resolved_at=?
               WHERE matchup_id=? AND status='open'""",
            (actor_id, now, matchup["id"]),
        )
        await conn.commit()
        return True


async def reopen_final_matchup(
    db: Database, matchup, actor_id: int
) -> bool:
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT * FROM matchups WHERE id=?", (matchup["id"],)
        )
        current = await cursor.fetchone()
        if not current or current["status"] not in FINAL_STATUSES:
            await conn.rollback()
            return False
        outcome = current["status"]
        if outcome == "complete":
            away_won = current["away_score"] > current["home_score"]
            winner = current["away_team"] if away_won else current["home_team"]
            loser = current["home_team"] if away_won else current["away_team"]
        elif outcome in ("force_away", "force_home"):
            winner = (
                current["away_team"] if outcome == "force_away" else current["home_team"]
            )
            loser = (
                current["home_team"] if outcome == "force_away" else current["away_team"]
            )
        else:
            winner = loser = None
        if winner:
            await conn.execute(
                """UPDATE teams SET wins=MAX(0,wins-1)
                   WHERE guild_id=? AND season=? AND lower(name)=lower(?)""",
                (current["guild_id"], current["season"], winner),
            )
            await conn.execute(
                """UPDATE teams SET losses=MAX(0,losses-1)
                   WHERE guild_id=? AND season=? AND lower(name)=lower(?)""",
                (current["guild_id"], current["season"], loser),
            )
        await reverse_matchup_progress(conn, current, outcome)
        cursor = await conn.execute(
            """UPDATE matchups SET status='waiting',away_score=NULL,home_score=NULL,
               scheduled_at=NULL,schedule_previous_at=NULL,proposed_by=NULL,
               proposed_at=NULL,schedule_proposal_version=schedule_proposal_version+1,
               result_submission_version=result_submission_version+1,
               result_submitted_by=NULL,result_submitted_at=NULL,
               result_evidence_url=NULL,result_opponent_status=NULL,
               result_opponent_by=NULL,result_audit_message_id=NULL,
               result_reviewed_by=?,result_reviewed_at=?,
               result_review_note='Previous official result was reopened',
               issue_text=NULL,updated_at=? WHERE id=? AND status=?""",
            (
                actor_id,
                iso_now(),
                iso_now(),
                current["id"],
                outcome,
            ),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return False
        await conn.commit()
        return True


async def publish_reopened_matchup(client: discord.Client, previous) -> None:
    updated = await client.db.fetchone(
        "SELECT * FROM matchups WHERE id=?", (previous["id"],)
    )
    if not updated:
        return
    guild = client.get_guild(previous["guild_id"])
    if not guild:
        return
    settings = await client.db.settings(previous["guild_id"])
    from .channel_workflow import matchup_channel_embed, REACTIONS

    embed = await matchup_channel_embed(client.db, updated, settings, guild)
    channel = guild.get_channel(previous["channel_id"] or 0)
    if isinstance(channel, discord.TextChannel) and previous["message_id"]:
        try:
            message = await channel.fetch_message(previous["message_id"])
            await message.edit(embed=embed, view=None)
            for target in list(channel.overwrites):
                if isinstance(target, discord.Role) and not target.is_default():
                    await channel.set_permissions(
                        target,
                        view_channel=True,
                        send_messages=True,
                        add_reactions=True,
                        read_message_history=True,
                    )
            for emoji in REACTIONS:
                await message.add_reaction(emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    notice = discord.Embed(
        title="Official Result Reopened",
        description=(
            "A Commissioner reversed the previous result, standings, and XP. "
            "This matchup is active again."
        ),
        color=discord.Color.red(),
    )
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=notice)
        except (discord.Forbidden, discord.HTTPException):
            pass
    audit = guild.get_channel(settings.get("audit_channel_id") or 0)
    if isinstance(audit, discord.TextChannel):
        try:
            await audit.send(embed=notice)
        except (discord.Forbidden, discord.HTTPException):
            pass


def commissioner_matchup_embed(matchup) -> discord.Embed:
    embed = discord.Embed(
        title=f"Manage #{matchup['id']} · {matchup['away_team']} @ {matchup['home_team']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Current Status",
        value=status_label(matchup["status"], matchup["deadline_at"]),
        inline=False,
    )
    if matchup["away_score"] is not None and matchup["home_score"] is not None:
        embed.add_field(
            name="Submitted Score",
            value=f"{matchup['away_team']} {matchup['away_score']} - "
            f"{matchup['home_score']} {matchup['home_team']}",
            inline=False,
        )
    if matchup["result_opponent_status"]:
        embed.add_field(
            name="Opponent Review",
            value=matchup["result_opponent_status"].replace("_", " ").title(),
        )
    if matchup["issue_text"]:
        embed.add_field(name="Issue", value=matchup["issue_text"][:1024], inline=False)
    embed.set_footer(text="Important decisions require a separate confirmation.")
    return embed


def outcome_description(matchup, action: str) -> str:
    if action == "force_away":
        return f"Force win awarded to {matchup['away_team']}."
    if action == "force_home":
        return f"Force win awarded to {matchup['home_team']}."
    return "Matchup marked for a neutral fair simulation."


async def send_audit_notice(
    interaction: discord.Interaction,
    matchup,
    title: str,
    description: str,
) -> None:
    settings = await interaction.client.db.settings(matchup["guild_id"])
    guild = interaction.client.get_guild(matchup["guild_id"])
    channel = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(
                embed=discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.blurple(),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def publish_outcome(client: discord.Client, matchup, action: str) -> None:
    guild = client.get_guild(matchup["guild_id"])
    if guild is None:
        return
    settings = await client.db.settings(matchup["guild_id"])
    description = outcome_description(matchup, action)
    away_display = await team_label(client.db, guild, matchup["season"], matchup["away_team"] )
    home_display = await team_label(client.db, guild, matchup["season"], matchup["home_team"] )
    embed = discord.Embed(
        title=f"Official Ruling · {matchup['away_team']} @ {matchup['home_team']}",
        description=f"{away_display} at {home_display}\n\n{description}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Official Commissioner decision")
    channel = guild.get_channel(matchup["channel_id"] or 0)
    if isinstance(channel, discord.TextChannel) and matchup["message_id"]:
        try:
            message = await channel.fetch_message(matchup["message_id"])
            await message.edit(embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass
    score_channel = guild.get_channel(settings.get("final_scores_channel_id") or 0)
    score_posted = False
    if (
        isinstance(score_channel, discord.TextChannel)
        and score_channel.id != getattr(channel, "id", None)
    ):
        if matchup["final_score_message_id"]:
            try:
                posted = score_channel.get_partial_message(matchup["final_score_message_id"])
                await posted.edit(embed=embed)
                score_posted = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if not score_posted:
            try:
                posted = await score_channel.send(embed=embed)
                await client.db.execute(
                    """UPDATE matchups SET final_score_message_id=?,
                       final_score_posted_at=?,updated_at=? WHERE id=?""",
                    (posted.id, iso_now(), iso_now(), matchup["id"]),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
    from .channel_workflow import lock_matchup_channel
    await lock_matchup_channel(client.db, guild, matchup)
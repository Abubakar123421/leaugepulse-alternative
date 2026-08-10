from __future__ import annotations

import re

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import FINAL_STATUSES, iso_now
from .progression import record_matchup_progress
from .team_emojis import team_label

ALLOWED_EVIDENCE_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


class ResultSubmissionModal(discord.ui.Modal, title="Submit Final Score"):
    def __init__(self, db: Database, matchup):
        super().__init__(
            timeout=600,
            custom_id=f"leaguebot:result:submit:{matchup['id']}",
        )
        self.db = db
        self.matchup_id = matchup["id"]
        self.away_score = discord.ui.TextInput(
            custom_id="leaguebot:result:away-score",
            placeholder="Enter the final score",
            min_length=1,
            max_length=3,
        )
        self.home_score = discord.ui.TextInput(
            custom_id="leaguebot:result:home-score",
            placeholder="Enter the final score",
            min_length=1,
            max_length=3,
        )
        self.add_item(discord.ui.Label(
            text=f"{matchup['away_team']} score"[:45], component=self.away_score
        ))
        self.add_item(discord.ui.Label(
            text=f"{matchup['home_team']} score"[:45], component=self.home_score
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            away_score = int(str(self.away_score))
            home_score = int(str(self.home_score))
            if min(away_score, home_score) < 0 or away_score == home_score:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Enter non-negative whole-number scores; league games cannot end tied.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.edit_original_response(content="This matchup no longer exists.")
            return

        settings = await self.db.settings(matchup["guild_id"])
        if (
            interaction.user.id
            not in (matchup["away_user_id"], matchup["home_user_id"])
            and not await is_commissioner(interaction, settings)
        ):
            await interaction.edit_original_response(
                content="Only the matchup owners or a commissioner can submit a result."
            )
            return

        audit_channel = _audit_channel(interaction.client, matchup, settings)
        if audit_channel is None:
            await interaction.edit_original_response(
                content="The commissioner audit channel is not configured. Ask an admin to rerun `/setup`."
            )
            return

        if matchup["status"] in FINAL_STATUSES:
            await interaction.edit_original_response(
                content="This matchup is already final."
            )
            return
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups
                   SET result_submission_version=result_submission_version+1,
                       updated_at=?
                   WHERE id=? AND status NOT IN
                       ('complete','force_home','force_away','fair_sim')
                   RETURNING result_submission_version""",
                (iso_now(), self.matchup_id),
            )
            version_row = await cursor.fetchone()
            await conn.commit()
        if not version_row:
            await interaction.edit_original_response(
                content="This matchup was finalized while you were submitting the result."
            )
            return
        submission_version = version_row[0]
        opponent_status = "not_required"
        submitted_at = iso_now()
        provisional = dict(matchup)
        provisional.update(
            {
                "away_score": away_score,
                "result_submission_version": submission_version,
                "home_score": home_score,
                "result_submitted_by": interaction.user.id,
                "result_submitted_at": submitted_at,
                "result_opponent_status": opponent_status,
                "result_opponent_by": None,
                "result_review_note": None,
                "status": "result_pending",
            }
        )
        mention = _commissioner_mention(settings)
        try:
            audit_message = await audit_channel.send(
                mention,
                embed=_result_embed(provisional),
                view=CommissionerResultReviewView(self.matchup_id, submission_version),
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.edit_original_response(
                content="I could not send this result to the commissioner review channel."
            )
            return
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET away_score=?,home_score=?,status='result_pending',
                   result_submitted_by=?,result_submitted_at=?,result_evidence_url=NULL,
                   result_opponent_status=?,result_opponent_by=NULL,
                   result_audit_message_id=?,result_reviewed_by=NULL,
                   result_reviewed_at=NULL,result_review_note=NULL,updated_at=?
                   WHERE id=? AND result_submission_version=?
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (
                    away_score,
                    home_score,
                    interaction.user.id,
                    submitted_at,
                    opponent_status,
                    audit_message.id,
                    iso_now(),
                    self.matchup_id,
                    submission_version,
                ),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            try:
                await audit_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            await interaction.edit_original_response(
                content=(
                    "A newer result submission or Commissioner decision replaced this one. "
                    "Open the current matchup before trying again."
                )
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "result_submitted",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={
                "away_score": away_score,
                "result_submission_version": submission_version,
                "home_score": home_score,
                "version": submission_version,
            },
        )

        updated = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        await audit_message.edit(
            embed=_result_embed(updated),
            view=CommissionerResultReviewView(self.matchup_id, submission_version),
        )
        from .channel_workflow import refresh_matchup_message
        await refresh_matchup_message(interaction.client, self.db, self.matchup_id)
        await interaction.edit_original_response(
            content=(
                f"**{matchup['away_team']} {away_score} - {home_score} "
                f"{matchup['home_team']}** was sent to the commissioners for review."
            )
        )


class MatchupSubmitScoreButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaguebot:matchup:submit:(?P<matchup_id>\d+)",
):
    def __init__(self, matchup_id: int, *, disabled: bool = False):
        self.matchup_id = matchup_id
        super().__init__(discord.ui.Button(
            label="Game Complete / Submit Score",
            style=discord.ButtonStyle.success,
            custom_id=f"leaguebot:matchup:submit:{matchup_id}",
            disabled=disabled,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["matchup_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db: Database = interaction.client.db
        matchup = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.response.send_message(
                "This matchup no longer exists.", ephemeral=True
            )
            return
        if interaction.user.id not in (
            matchup["away_user_id"], matchup["home_user_id"]
        ):
            await interaction.response.send_message(
                "Only the two assigned team owners can submit this score.", ephemeral=True
            )
            return
        if matchup["status"] in FINAL_STATUSES:
            await interaction.response.send_message(
                "This matchup is already final.", ephemeral=True
            )
            return
        if matchup["status"] in ("result_pending", "issue_reported"):
            await interaction.response.send_message(
                "A score is already waiting for commissioner review.", ephemeral=True
            )
            return
        await interaction.response.send_modal(ResultSubmissionModal(db, matchup))


class MatchupScoreSubmissionView(discord.ui.View):
    def __init__(self, matchup_id: int, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.add_item(MatchupSubmitScoreButton(matchup_id, disabled=disabled))


class OpponentResultDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"leaguebot:result:(?P<action>confirm|dispute):"
        r"(?P<matchup_id>\d+)(?::(?P<version>\d+))?"
    ),
):
    def __init__(self, action: str, matchup_id: int, version: int | None = None):
        self.action = action
        self.matchup_id = matchup_id
        self.version = version
        super().__init__(
            discord.ui.Button(
                label="Confirm Result" if action == "confirm" else "Dispute Result",
                style=(
                    discord.ButtonStyle.success
                    if action == "confirm"
                    else discord.ButtonStyle.danger
                ),
                custom_id=(
                    f"leaguebot:result:{action}:{matchup_id}"
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
    ) -> "OpponentResultDecisionButton":
        version = int(match["version"]) if match["version"] else None
        return cls(match["action"], int(match["matchup_id"]), version)

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        matchup = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if matchup and (
            (self.version is None and matchup["result_submission_version"] != 0)
            or (self.version is not None and self.version != matchup["result_submission_version"])
        ):
            await interaction.response.send_message(
                "This is an older result submission. Use the newest DM instead.",
                ephemeral=True,
            )
            return
        if not matchup or matchup["status"] != "result_pending":
            await interaction.response.send_message(
                "This result is no longer awaiting player confirmation.", ephemeral=True
            )
            return
        if interaction.user.id == matchup["result_submitted_by"]:
            await interaction.response.send_message(
                "You cannot confirm your own submission.", ephemeral=True
            )
            return
        if interaction.user.id not in (
            matchup["away_user_id"],
            matchup["home_user_id"],
        ):
            await interaction.response.send_message(
                "Only the opposing team owner can decide this result.", ephemeral=True
            )
            return

        await interaction.response.defer()
        opponent_status = "confirmed" if self.action == "confirm" else "disputed"
        new_status = "result_pending" if self.action == "confirm" else "issue_reported"
        issue_text = (
            matchup["issue_text"]
            if self.action == "confirm"
            else "The opposing team owner disputed the submitted score."
        )
        async with db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET result_opponent_status=?,result_opponent_by=?,
                   status=?,issue_text=?,updated_at=?
                   WHERE id=? AND status='result_pending'
                   AND result_submission_version=?""",
                (
                    opponent_status,
                    interaction.user.id,
                    new_status,
                    issue_text,
                    iso_now(),
                    self.matchup_id,
                    matchup["result_submission_version"],
                ),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            await interaction.edit_original_response(
                content="This result changed before your decision was saved.",
                embed=None,
                view=None,
            )
            return
        await db.audit(
            matchup["guild_id"],
            interaction.user.id,
            f"result_{opponent_status}",
            target_type="matchup",
            target_id=str(self.matchup_id),
        )
        updated = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        await _refresh_audit_message(interaction.client, updated)
        if self.action == "dispute":
            await _send_commissioner_notice(
                interaction.client,
                updated,
                "Result Disputed",
                (
                    f"<@{interaction.user.id}> disputed the submitted score for "
                    f"**{updated['away_team']} @ {updated['home_team']}**."
                ),
                discord.Color.red(),
            )
        await _dm_user(
            interaction.client,
            matchup["result_submitted_by"],
            (
                "✅ The opposing owner confirmed your submitted result."
                if self.action == "confirm"
                else "❌ The opposing owner disputed your submitted result. A Commissioner will review it."
            ),
        )
        await interaction.edit_original_response(
            content=(
                "You confirmed the submitted result. A Commissioner will make it official."
                if self.action == "confirm"
                else "You disputed the result. The matchup is flagged for Commissioner review."
            ),
            embed=None,
            view=None,
        )


class OpponentResultDecisionView(discord.ui.View):
    def __init__(self, matchup_id: int, version: int | None = None):
        super().__init__(timeout=None)
        self.add_item(OpponentResultDecisionButton("confirm", matchup_id, version))
        self.add_item(OpponentResultDecisionButton("dispute", matchup_id, version))


class CommissionerResultActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"leaguebot:result:staff:(?P<action>approve|reject|edit|evidence):"
        r"(?P<matchup_id>\d+)(?::(?P<version>\d+))?"
    ),
):
    def __init__(self, action: str, matchup_id: int, version: int | None = None):
        self.action = action
        self.matchup_id = matchup_id
        self.version = version
        labels = {
            "approve": "Approve Result",
            "reject": "Reject Result",
            "edit": "Edit Score",
            "evidence": "Request More Evidence",
        }
        styles = {
            "approve": discord.ButtonStyle.success,
            "reject": discord.ButtonStyle.danger,
            "edit": discord.ButtonStyle.primary,
            "evidence": discord.ButtonStyle.secondary,
        }
        super().__init__(
            discord.ui.Button(
                label=labels[action],
                style=styles[action],
                custom_id=(
                    f"leaguebot:result:staff:{action}:{matchup_id}"
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
    ) -> "CommissionerResultActionButton":
        version = int(match["version"]) if match["version"] else None
        return cls(match["action"], int(match["matchup_id"]), version)

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        matchup = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if matchup and (
            (self.version is None and matchup["result_submission_version"] != 0)
            or (self.version is not None and self.version != matchup["result_submission_version"])
        ):
            await interaction.response.send_message(
                "This audit card belongs to an older result submission.", ephemeral=True
            )
            return
        if not matchup or matchup["status"] not in ("result_pending", "issue_reported"):
            await interaction.response.send_message(
                "This result is no longer awaiting Commissioner review.", ephemeral=True
            )
            return
        if not matchup["result_submitted_by"]:
            await interaction.response.send_message(
                "This is an incomplete submission from an earlier failed attempt. "
                "Ask a team owner to submit the score again.",
                ephemeral=True,
            )
            return
        settings = await db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only a Commissioner can review this result.", ephemeral=True
            )
            return
        if self.action == "edit":
            await interaction.response.send_modal(
                CommissionerScoreEditModal(db, matchup, self.version)
            )
            return
        if self.action == "evidence":
            await interaction.response.send_modal(
                MoreEvidenceModal(db, self.matchup_id, self.version)
            )
            return
        warning = None
        if self.action == "approve" and matchup["result_opponent_status"] == "disputed":
            warning = "The opposing owner disputed this result. Approval will override that dispute."
        await interaction.response.send_message(
            content=warning or f"Confirm **{self.action} result** for matchup #{self.matchup_id}.",
            view=CommissionerResultConfirmationView(
                db, self.matchup_id, self.action, interaction.user.id, self.version
            ),
            ephemeral=True,
        )


class CommissionerScoreEditModal(discord.ui.Modal, title="Edit Submitted Score"):
    def __init__(self, db: Database, matchup, version: int | None):
        super().__init__(
            timeout=600,
            custom_id=f"leaguebot:result:staff-edit:{matchup['id']}:{version or 0}",
        )
        self.db = db
        self.matchup_id = matchup["id"]
        self.version = version
        self.away_score = discord.ui.TextInput(
            custom_id="leaguebot:result:edit-away-score",
            default=str(matchup["away_score"]),
            max_length=3,
        )
        self.home_score = discord.ui.TextInput(
            custom_id="leaguebot:result:edit-home-score",
            default=str(matchup["home_score"]),
            max_length=3,
        )
        self.add_item(discord.ui.Label(
            text=f"{matchup['away_team']} score"[:45], component=self.away_score
        ))
        self.add_item(discord.ui.Label(
            text=f"{matchup['home_team']} score"[:45], component=self.home_score
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            away_score = int(str(self.away_score))
            home_score = int(str(self.home_score))
            if min(away_score, home_score) < 0 or away_score == home_score:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Enter non-negative whole-number scores; league games cannot end tied.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup or matchup["status"] not in ("result_pending", "issue_reported"):
            await interaction.edit_original_response(
                content="This result is no longer awaiting review."
            )
            return
        if self.version is not None and self.version != matchup["result_submission_version"]:
            await interaction.edit_original_response(
                content="This edit belongs to an older score submission."
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.edit_original_response(
                content="Only a Commissioner can edit submitted scores."
            )
            return
        old_scores = (matchup["away_score"], matchup["home_score"])
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET away_score=?,home_score=?,result_review_note=?,updated_at=?
                   WHERE id=? AND result_submission_version=?
                   AND status IN ('result_pending','issue_reported')""",
                (
                    away_score,
                    home_score,
                    f"Score edited by Commissioner <@{interaction.user.id}>",
                    iso_now(),
                    self.matchup_id,
                    matchup["result_submission_version"],
                ),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            await interaction.edit_original_response(
                content="The result changed before this edit was saved."
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "result_score_edited",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={
                "old_away_score": old_scores[0],
                "old_home_score": old_scores[1],
                "away_score": away_score,
                "home_score": home_score,
            },
        )
        updated = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        await _refresh_audit_message(interaction.client, updated)
        from .channel_workflow import refresh_matchup_message
        await refresh_matchup_message(interaction.client, self.db, self.matchup_id)
        await interaction.edit_original_response(
            content=(
                f"Score updated to **{updated['away_team']} {away_score} - "
                f"{home_score} {updated['home_team']}**."
            )
        )


class CommissionerResultReviewView(discord.ui.View):
    def __init__(self, matchup_id: int, version: int | None = None):
        super().__init__(timeout=None)
        self.add_item(CommissionerResultActionButton("approve", matchup_id, version))
        self.add_item(CommissionerResultActionButton("edit", matchup_id, version))
        self.add_item(CommissionerResultActionButton("reject", matchup_id, version))


class CommissionerResultConfirmationView(discord.ui.View):
    def __init__(
        self, db: Database, matchup_id: int, action: str, actor_id: int,
        version: int | None = None,
    ):
        super().__init__(timeout=120)
        self.db = db
        self.matchup_id = matchup_id
        self.action = action
        self.actor_id = actor_id
        self.version = version

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
        if matchup and self.version is not None and (
            self.version != matchup["result_submission_version"]
        ):
            await interaction.edit_original_response(
                content="This confirmation belongs to an older result submission.",
                view=None,
            )
            return
        if not matchup or matchup["status"] not in ("result_pending", "issue_reported"):
            await interaction.edit_original_response(
                content="This result has already been handled.", view=None
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.edit_original_response(
                content="Your Commissioner access was removed.", view=None
            )
            return
        applied = await _apply_result_decision(
            self.db, matchup, self.action, interaction.user.id
        )
        if not applied:
            await interaction.edit_original_response(
                content="Another Commissioner already handled this result.", view=None
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "result_approved" if self.action == "approve" else "result_rejected",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={
                "away_score": matchup["away_score"],
                "home_score": matchup["home_score"],
            },
        )
        updated = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        await _refresh_audit_message(interaction.client, updated, final=True)
        published = True
        if self.action == "approve":
            published = await _publish_final_result(interaction.client, updated)
            from .weekly_content import publish_weekly_recap
            await publish_weekly_recap(
                interaction.client, self.db, updated["guild_id"], updated["season"],
                updated["week"], create=False, regenerate_ai=True,
            )
            ai = getattr(interaction.client, "ai", None)
            if ai and ai.available:
                ai.enqueue(
                    guild_id=updated["guild_id"], season=updated["season"],
                    source_key=f"matchup:{updated['id']}", kind="recap",
                    prompt=(
                        "Write a concise Madden game recap under 250 words using only this final score; "
                        "do not invent players, stats, drives, quotes, or events. "
                        f"{updated['away_team']} {updated['away_score']}, "
                        f"{updated['home_team']} {updated['home_score']}."
                    ),
                    channel_id=updated["channel_id"], title="\N{NEWSPAPER} Official Game Recap",
                )
        else:
            from .channel_workflow import refresh_matchup_message
            await refresh_matchup_message(
                interaction.client, self.db, updated["id"]
            )
        owners = {
            user_id
            for user_id in (matchup["away_user_id"], matchup["home_user_id"])
            if user_id
        }
        message = (
            f"✅ Commissioner approved the final: **{matchup['away_team']} "
            f"{matchup['away_score']} - {matchup['home_score']} {matchup['home_team']}**."
            if self.action == "approve"
            else "❌ Commissioner rejected the submitted result. Submit a corrected score with new evidence."
        )
        if self.action == "approve" and not published:
            message += (
                " Public matchup post could not be updated; check the bot's channel permissions."
            )
        for owner_id in owners:
            await _dm_user(interaction.client, owner_id, message)
        button.disabled = True
        await interaction.edit_original_response(content=message, view=self)


class MoreEvidenceModal(discord.ui.Modal, title="Request More Evidence"):
    reason = discord.ui.TextInput(
        label="What additional evidence is needed?",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500,
    )

    def __init__(self, db: Database, matchup_id: int, version: int | None = None):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:result:evidence-request:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id
        self.version = version

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not matchup:
            await interaction.edit_original_response(content="This matchup no longer exists.")
            return
        if matchup and self.version is not None and (
            self.version != matchup["result_submission_version"]
        ):
            await interaction.edit_original_response(
                content="This request belongs to an older result submission."
            )
            return
        settings = await self.db.settings(matchup["guild_id"])
        if not await is_commissioner(interaction, settings):
            await interaction.edit_original_response(
                content="Only a Commissioner can request more evidence."
            )
            return
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """UPDATE matchups SET result_review_note=?,updated_at=?
                   WHERE id=? AND result_submission_version=?
                   AND status IN ('result_pending','issue_reported')""",
                (
                    str(self.reason),
                    iso_now(),
                    self.matchup_id,
                    matchup["result_submission_version"],
                ),
            )
            await conn.commit()
        if cursor.rowcount != 1:
            await interaction.edit_original_response(
                content="The result changed before this request was saved."
            )
            return
        await self.db.audit(
            matchup["guild_id"],
            interaction.user.id,
            "result_more_evidence_requested",
            target_type="matchup",
            target_id=str(self.matchup_id),
            details={"reason": str(self.reason)},
        )
        await _dm_user(
            interaction.client,
            matchup["result_submitted_by"],
            f"Commissioner requested more evidence for your result: {self.reason}",
        )
        updated = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        await _refresh_audit_message(interaction.client, updated)
        await interaction.edit_original_response(
            content="The submitter was privately asked for more evidence."
        )


def _result_embed(matchup, evidence_url: str | None = None) -> discord.Embed:
    status = matchup["result_opponent_status"] or "pending"
    away_score = matchup["away_score"]
    home_score = matchup["home_score"]
    if away_score is not None and home_score is not None:
        away_won = away_score > home_score
        winner = matchup["away_team"] if away_won else matchup["home_team"]
        loser = matchup["home_team"] if away_won else matchup["away_team"]
    else:
        winner = loser = "Pending"
    embed = discord.Embed(
        title=f"Result Review · Week {matchup['week']}",
        description=(
            f"**Matchup:** {matchup['away_team']} @ {matchup['home_team']}\n"
            f"**Final score:** {matchup['away_team']} {away_score} - "
            f"{home_score} {matchup['home_team']}\n"
            f"**Winner:** {winner}\n"
            f"**Loser:** {loser}\n"
            f"**Submitted by:** <@{matchup['result_submitted_by']}>"
        ),
        color=(
            discord.Color.red()
            if status == "disputed"
            else discord.Color.green()
            if matchup["status"] == "complete"
            else discord.Color.gold()
        ),
    )
    if matchup["result_review_note"]:
        embed.add_field(
            name="Commissioner Note",
            value=matchup["result_review_note"][:1024],
            inline=False,
        )
    image_url = evidence_url or matchup["result_evidence_url"]
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(
        text=f"Review status: {matchup['status'].replace('_', ' ').title()}"
    )
    return embed


async def _notify_opponents(
    client: discord.Client, matchup, opponent_ids: set[int], version: int
) -> bool:
    if not opponent_ids:
        return False
    embed = discord.Embed(
        title="Confirm or Dispute Final Score",
        description=(
            f"<@{matchup['result_submitted_by']}> submitted:\n\n"
            f"**{matchup['away_team']} {matchup['away_score']} - "
            f"{matchup['home_score']} {matchup['home_team']}**"
        ),
        color=discord.Color.gold(),
    )
    embed.set_image(url=matchup["result_evidence_url"])
    sent = False
    for user_id in opponent_ids:
        user = await _get_user(client, user_id)
        if not user:
            continue
        try:
            await user.send(
                embed=embed,
                view=OpponentResultDecisionView(matchup["id"], version),
            )
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            continue
    return sent


async def restore_pending_result_reviews(
    client: discord.Client, db: Database, guild_id: int
) -> int:
    rows = await db.fetchall(
        """SELECT * FROM matchups WHERE guild_id=?
           AND status IN ('result_pending','issue_reported')
           AND result_audit_message_id IS NOT NULL""",
        (guild_id,),
    )
    repaired = 0
    for matchup in rows:
        await _refresh_audit_message(client, matchup)
        repaired += 1
    return repaired

async def submit_result_from_message(
    client: discord.Client,
    db: Database,
    matchup,
    settings: dict,
    submitted_by: int,
    away_score: int,
    home_score: int,
    attachment: discord.Attachment,
) -> tuple[bool, str]:
    """Submit a reaction-prompt result through the same review flow as the modal."""
    audit_channel = _audit_channel(client, matchup, settings)
    if audit_channel is None:
        return False, "The commissioner audit channel is missing. Run `/setup`."
    if matchup["status"] in FINAL_STATUSES:
        return False, "This matchup is already final."

    async with db.connect() as conn:
        cursor = await conn.execute(
            """UPDATE matchups
               SET result_submission_version=result_submission_version+1,
                   updated_at=?
               WHERE id=? AND status NOT IN
                   ('complete','force_home','force_away','fair_sim')
               RETURNING result_submission_version""",
            (iso_now(), matchup["id"]),
        )
        version_row = await cursor.fetchone()
        await conn.commit()
    if not version_row:
        return False, "This matchup was finalized while you were submitting the result."

    submission_version = version_row[0]
    opponent_ids = {
        user_id
        for user_id in (matchup["away_user_id"], matchup["home_user_id"])
        if user_id and user_id != submitted_by
    }
    opponent_status = "pending" if opponent_ids else "unavailable"
    filename = (
        f"matchup-{matchup['id']}-result-v{submission_version}."
        f"{_extension(attachment)}"
    )
    uploaded_file = await attachment.to_file(filename=filename)
    submitted_at = iso_now()
    provisional = dict(matchup)
    provisional.update(
        {
            "away_score": away_score,
            "home_score": home_score,
            "result_submission_version": submission_version,
            "result_submitted_by": submitted_by,
            "result_submitted_at": submitted_at,
            "result_opponent_status": opponent_status,
            "result_opponent_by": None,
            "result_review_note": None,
            "status": "result_pending",
        }
    )
    audit_message = await audit_channel.send(
        _commissioner_mention(settings),
        embed=_result_embed(provisional, evidence_url=f"attachment://{filename}"),
        file=uploaded_file,
        view=CommissionerResultReviewView(matchup["id"], submission_version),
        allowed_mentions=discord.AllowedMentions(
            roles=True, users=False, everyone=False
        ),
    )
    evidence_url = await _attachment_url(
        audit_channel, audit_message, attachment.url
    )
    async with db.connect() as conn:
        cursor = await conn.execute(
            """UPDATE matchups SET away_score=?,home_score=?,status='result_pending',
               result_submitted_by=?,result_submitted_at=?,result_evidence_url=?,
               result_opponent_status=?,result_opponent_by=NULL,
               result_audit_message_id=?,result_reviewed_by=NULL,
               result_reviewed_at=NULL,result_review_note=NULL,updated_at=?
               WHERE id=? AND result_submission_version=?
               AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (
                away_score,
                home_score,
                submitted_by,
                submitted_at,
                evidence_url,
                opponent_status,
                audit_message.id,
                iso_now(),
                matchup["id"],
                submission_version,
            ),
        )
        await conn.commit()
    if cursor.rowcount != 1:
        try:
            await audit_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        return False, "A newer result or Commissioner decision replaced this submission."

    await db.audit(
        matchup["guild_id"],
        submitted_by,
        "result_submitted",
        target_type="matchup",
        target_id=str(matchup["id"]),
        details={
            "away_score": away_score,
            "home_score": home_score,
            "evidence_url": evidence_url,
            "submission_version": submission_version,
        },
    )
    updated = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup["id"],))
    await audit_message.edit(
        embed=_result_embed(updated),
        view=CommissionerResultReviewView(matchup["id"], submission_version),
    )
    notified = await _notify_opponents(
        client, updated, opponent_ids, submission_version
    )
    notice = (
        "The opposing owner was also sent a private Confirm / Dispute request."
        if notified
        else "The Commissioner review card is ready in the audit channel."
    )
    return True, notice

async def _refresh_audit_message(
    client: discord.Client, matchup, *, final: bool = False
) -> None:
    settings = await client.db.settings(matchup["guild_id"])
    channel = _audit_channel(client, matchup, settings)
    if channel is None or not matchup["result_audit_message_id"]:
        return
    try:
        message = await channel.fetch_message(matchup["result_audit_message_id"])
        await message.edit(
            embed=_result_embed(matchup),
            view=(
                None
                if final
                else CommissionerResultReviewView(
                    matchup["id"], matchup["result_submission_version"]
                )
            ),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def _send_commissioner_notice(
    client: discord.Client,
    matchup,
    title: str,
    description: str,
    color: discord.Color,
) -> None:
    settings = await client.db.settings(matchup["guild_id"])
    channel = _audit_channel(client, matchup, settings)
    if channel:
        await channel.send(
            _commissioner_mention(settings),
            embed=discord.Embed(title=title, description=description, color=color),
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False
            ),
        )


def _audit_channel(
    client: discord.Client, matchup, settings: dict
) -> discord.TextChannel | None:
    guild = client.get_guild(matchup["guild_id"])
    channel = (
        guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    )
    return channel if isinstance(channel, discord.TextChannel) else None


def _commissioner_mention(settings: dict) -> str:
    return (
        f"<@&{settings['commissioner_role_id']}>"
        if settings.get("commissioner_role_id")
        else "Commissioners"
    )


async def _get_user(client: discord.Client, user_id: int) -> discord.User | None:
    user = client.get_user(user_id)
    if user:
        return user
    try:
        return await client.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def _dm_user(client: discord.Client, user_id: int | None, message: str) -> None:
    if not user_id:
        return
    user = await _get_user(client, user_id)
    if not user:
        return
    try:
        await user.send(message)
    except (discord.Forbidden, discord.HTTPException):
        pass


def _extension(attachment: discord.Attachment) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }[attachment.content_type]

async def _attachment_url(
    channel: discord.TextChannel,
    message: discord.Message,
    fallback_url: str,
) -> str:
    if message.attachments:
        return message.attachments[0].url
    try:
        refreshed = await channel.fetch_message(message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return fallback_url
    return refreshed.attachments[0].url if refreshed.attachments else fallback_url

async def _apply_result_decision(
    db: Database,
    matchup,
    action: str,
    actor_id: int,
) -> bool:
    """Apply a review once; approval also updates both team records atomically."""
    new_status = "complete" if action == "approve" else "waiting"
    now = iso_now()
    async with db.connect() as conn:
        cursor = await conn.execute(
            """UPDATE matchups SET status=?, result_reviewed_by=?,
               result_reviewed_at=?, result_review_note=?, updated_at=?
               WHERE id=? AND status IN ('result_pending','issue_reported')
               AND result_submitted_by IS NOT NULL
               AND result_submission_version=?""",
            (
                new_status, actor_id, now, action.title(), now,
                matchup["id"], matchup["result_submission_version"],
            ),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            return False
        if action == "approve":
            for team_name in (matchup["away_team"], matchup["home_team"]):
                await conn.execute(
                    """INSERT INTO teams (guild_id,season,name)
                       VALUES (?,?,?)
                       ON CONFLICT(guild_id,season,name) DO NOTHING""",
                    (matchup["guild_id"], matchup["season"], team_name),
                )
            away_won = matchup["away_score"] > matchup["home_score"]
            winner = matchup["away_team"] if away_won else matchup["home_team"]
            loser = matchup["home_team"] if away_won else matchup["away_team"]
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
            await record_matchup_progress(conn, matchup, "complete")
        await conn.execute(
            """UPDATE matchup_cases SET status='resolved',
               resolution='Matchup finalized by result review',resolved_by=?,resolved_at=?
               WHERE matchup_id=? AND status='open'""",
            (actor_id, now, matchup["id"]),
        )
        await conn.commit()
        return True


async def _publish_final_result(client: discord.Client, matchup) -> bool:
    guild = client.get_guild(matchup["guild_id"])
    if guild is None:
        return False
    settings = await client.db.settings(matchup["guild_id"])
    embed = await _public_final_embed(client.db, matchup, guild)
    matchup_channel = guild.get_channel(matchup["channel_id"] or 0)
    original_updated = False
    if isinstance(matchup_channel, discord.TextChannel):
        if matchup["message_id"]:
            try:
                message = await matchup_channel.fetch_message(matchup["message_id"])
                await message.edit(embed=embed, view=None)
                original_updated = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    score_channel = guild.get_channel(settings.get("final_scores_channel_id") or 0)
    score_posted = False
    if (
        isinstance(score_channel, discord.TextChannel)
        and score_channel.id != getattr(matchup_channel, "id", None)
    ):
        if matchup["final_score_message_id"]:
            try:
                existing = score_channel.get_partial_message(matchup["final_score_message_id"])
                await existing.edit(embed=embed)
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
                score_posted = True
            except (discord.Forbidden, discord.HTTPException):
                pass
    from .channel_workflow import lock_matchup_channel
    await lock_matchup_channel(client.db, guild, matchup)
    return score_posted or original_updated


async def _public_final_embed(db: Database, matchup, guild: discord.Guild | None = None) -> discord.Embed:
    away_team = await db.fetchone(
        """SELECT * FROM teams WHERE guild_id=? AND season=?
           AND lower(name)=lower(?)""",
        (matchup["guild_id"], matchup["season"], matchup["away_team"]),
    )
    home_team = await db.fetchone(
        """SELECT * FROM teams WHERE guild_id=? AND season=?
           AND lower(name)=lower(?)""",
        (matchup["guild_id"], matchup["season"], matchup["home_team"]),
    )
    away_won = matchup["away_score"] > matchup["home_score"]
    winner_name = matchup["away_team"] if away_won else matchup["home_team"]
    winner_team = away_team if away_won else home_team
    away_display = await team_label(db, guild, matchup["season"], matchup["away_team"]) if guild else matchup["away_team"]
    home_display = await team_label(db, guild, matchup["season"], matchup["home_team"]) if guild else matchup["home_team"]
    winner_display = away_display if away_won else home_display
    away_owner = (
        f"<@{matchup['away_user_id']}>" if matchup["away_user_id"] else "Unassigned"
    )
    home_owner = (
        f"<@{matchup['home_user_id']}>" if matchup["home_user_id"] else "Unassigned"
    )

    def record(team) -> str:
        return f"{team['wins']}-{team['losses']}-{team['ties']}" if team else "0-0-0"

    embed = discord.Embed(
        title=f"Final · Week {matchup['week']} · {matchup['away_team']} @ {matchup['home_team']}",
        description=f"🏆 **{winner_display} wins!**",
        color=discord.Color.green(),
    )
    embed.add_field(
        name=away_display,
        value=f"**{matchup['away_score']}** points\n{away_owner}\nRecord: {record(away_team)}",
    )
    embed.add_field(
        name=home_display,
        value=f"**{matchup['home_score']}** points\n{home_owner}\nRecord: {record(home_team)}",
    )

    embed.set_footer(text="Official result approved by a league Commissioner")
    return embed

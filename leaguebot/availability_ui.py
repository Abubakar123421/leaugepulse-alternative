from __future__ import annotations

from datetime import UTC, datetime

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import FINAL_STATUSES, iso_now, parse_user_datetime


class AvailabilityHelpModal(discord.ui.Modal, title="Availability Help"):
    reason = discord.ui.TextInput(
        label="What changed?",
        placeholder="I cannot make the accepted time because…",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=800,
    )
    requested_deadline = discord.ui.TextInput(
        label="Requested extension (optional)",
        placeholder="2026-08-18 21:00",
        required=False,
        max_length=40,
    )

    def __init__(self, db: Database, matchup_id: int, timezone: str):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:availability:help:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id
        self.timezone = timezone

    async def on_submit(self, interaction: discord.Interaction) -> None:
        requested = None
        if str(self.requested_deadline).strip():
            try:
                requested = parse_user_datetime(
                    str(self.requested_deadline), self.timezone
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            if requested <= datetime.now(UTC):
                await interaction.response.send_message(
                    "The requested extension must be in the future.", ephemeral=True
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not _is_active_owner(matchup, interaction.user.id):
            await interaction.edit_original_response(
                content="Only an assigned owner in this active matchup can request availability help."
            )
            return
        case_id = await create_or_update_case(
            self.db,
            matchup=matchup,
            opened_by=interaction.user.id,
            kind="availability",
            reason=str(self.reason),
            requested_deadline_at=requested.isoformat() if requested else None,
        )
        await _notify_case(
            interaction,
            matchup,
            case_id,
            title="Player Availability Help",
            details=(
                f"**Player:** <@{interaction.user.id}>\n"
                f"**Reason:** {self.reason}\n"
                f"**Requested extension:** "
                + (
                    f"<t:{int(requested.timestamp())}:F>"
                    if requested
                    else "No specific deadline supplied"
                )
            ),
        )
        await interaction.edit_original_response(
            content=(
                "Your availability request was sent privately to the Commissioners. "
                "You can still use **Schedule Game** to propose a replacement time."
            )
        )


class ConcessionRequestModal(discord.ui.Modal, title="Concede This Game"):
    reason = discord.ui.TextInput(
        label="Why are you conceding?",
        placeholder="I cannot play before the advance deadline because…",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=800,
    )

    def __init__(self, db: Database, matchup_id: int):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:availability:concede:{matchup_id}",
        )
        self.db = db
        self.matchup_id = matchup_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        matchup = await self.db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (self.matchup_id,)
        )
        if not _is_active_owner(matchup, interaction.user.id):
            await interaction.edit_original_response(
                content="Only an assigned owner in this active matchup can concede."
            )
            return
        opponent_id = (
            matchup["home_user_id"]
            if interaction.user.id == matchup["away_user_id"]
            else matchup["away_user_id"]
        )
        if not opponent_id:
            await interaction.edit_original_response(
                content=(
                    "The opposing team has no approved owner. Report the issue instead "
                    "so a Commissioner can resolve the matchup."
                )
            )
            return
        case_id = await create_or_update_case(
            self.db,
            matchup=matchup,
            opened_by=interaction.user.id,
            kind="concession",
            reason=str(self.reason),
        )
        await _notify_case(
            interaction,
            matchup,
            case_id,
            title="Concession Awaiting Approval",
            details=(
                f"**Conceding owner:** <@{interaction.user.id}>\n"
                f"**Opponent:** <@{opponent_id}>\n"
                f"**Reason:** {self.reason}\n\n"
                "No result changes until a Commissioner approves this concession."
            ),
        )
        await _dm_user(
            interaction.client,
            opponent_id,
            (
                f"<@{interaction.user.id}> requested to concede **"
                f"{matchup['away_team']} @ {matchup['home_team']}**. "
                "A Commissioner must approve it before the result is official."
            ),
        )
        await interaction.edit_original_response(
            content=(
                "Your concession request is awaiting Commissioner approval. "
                "It is not an official loss yet."
            )
        )


class CaseResolutionView(discord.ui.View):
    def __init__(self, db: Database, case, actor_id: int):
        super().__init__(timeout=600)
        self.db = db
        self.case_id = case["id"]
        self.actor_id = actor_id
        if case["kind"] != "concession":
            self.approve_concession.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This case review belongs to another Commissioner.", ephemeral=True
            )
            return False
        case = await self.db.fetchone(
            "SELECT guild_id FROM matchup_cases WHERE id=?", (self.case_id,)
        )
        settings = await self.db.settings(case["guild_id"]) if case else {}
        if case and await is_commissioner(interaction, settings):
            return True
        await interaction.response.send_message(
            "Only a Commissioner can resolve this case.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Grant 24h Extension",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def extend_24h(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Confirm a 24-hour deadline extension?",
            view=CaseActionConfirmationView(
                self.db, self.case_id, "extend_24h", interaction.user.id
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Set Custom Deadline",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def custom_deadline(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        case = await self.db.fetchone(
            "SELECT * FROM matchup_cases WHERE id=?", (self.case_id,)
        )
        settings = await self.db.settings(case["guild_id"])
        await interaction.response.send_modal(
            CustomDeadlineModal(
                self.db, self.case_id, settings["timezone"]
            )
        )

    @discord.ui.button(
        label="Reopen Scheduling",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def reopen(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Confirm reopening scheduling and clearing the accepted time?",
            view=CaseActionConfirmationView(
                self.db, self.case_id, "reopen", interaction.user.id
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Approve Concession",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def approve_concession(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Confirm this concession and award the opponent a force win?",
            view=CaseActionConfirmationView(
                self.db, self.case_id, "concession", interaction.user.id
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Dismiss Case",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def dismiss(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Confirm dismissing this case with no matchup change?",
            view=CaseActionConfirmationView(
                self.db, self.case_id, "dismiss", interaction.user.id
            ),
            ephemeral=True,
        )


class CaseActionConfirmationView(discord.ui.View):
    def __init__(self, db: Database, case_id: int, action: str, actor_id: int):
        super().__init__(timeout=120)
        self.db = db
        self.case_id = case_id
        self.action = action
        self.actor_id = actor_id

    @discord.ui.button(label="Confirm Case Decision", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation belongs to another Commissioner.", ephemeral=True
            )
            return
        case = await self.db.fetchone(
            "SELECT guild_id FROM matchup_cases WHERE id=?", (self.case_id,)
        )
        settings = await self.db.settings(case["guild_id"]) if case else {}
        if not case or not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Your Commissioner access was removed.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            message = await resolve_case(
                interaction,
                self.db,
                self.case_id,
                self.action,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        button.disabled = True
        await interaction.edit_original_response(content=message, view=self)


class CustomDeadlineModal(discord.ui.Modal, title="Set Matchup Deadline"):
    deadline = discord.ui.TextInput(
        label="New deadline",
        placeholder="2026-08-18 21:00",
        max_length=40,
    )

    def __init__(self, db: Database, case_id: int, timezone: str):
        super().__init__(
            timeout=300,
            custom_id=f"leaguebot:case:deadline:{case_id}",
        )
        self.db = db
        self.case_id = case_id
        self.timezone = timezone

    async def on_submit(self, interaction: discord.Interaction) -> None:
        case = await self.db.fetchone(
            "SELECT guild_id FROM matchup_cases WHERE id=?", (self.case_id,)
        )
        settings = await self.db.settings(case["guild_id"]) if case else {}
        if not case or not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Your Commissioner access was removed.", ephemeral=True
            )
            return
        try:
            deadline = parse_user_datetime(str(self.deadline), self.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if deadline <= datetime.now(UTC):
            await interaction.response.send_message(
                "The new deadline must be in the future.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await resolve_case(
                interaction,
                self.db,
                self.case_id,
                "custom_deadline",
                custom_deadline=deadline.isoformat(),
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await interaction.edit_original_response(content=message)


async def create_or_update_case(
    db: Database,
    *,
    matchup,
    opened_by: int,
    kind: str,
    reason: str,
    requested_deadline_at: str | None = None,
) -> int:
    existing = await db.fetchone(
        """SELECT id FROM matchup_cases
           WHERE matchup_id=? AND opened_by=? AND kind=? AND status='open'
           ORDER BY id DESC LIMIT 1""",
        (matchup["id"], opened_by, kind),
    )
    if existing:
        await db.execute(
            """UPDATE matchup_cases
               SET reason=?,requested_deadline_at=?,created_at=?
               WHERE id=?""",
            (reason, requested_deadline_at, iso_now(), existing["id"]),
        )
        return existing["id"]
    return await db.execute(
        """INSERT INTO matchup_cases
           (matchup_id,guild_id,season,opened_by,kind,reason,
            requested_deadline_at,status,created_at)
           VALUES (?,?,?,?,?,?,?,'open',?)""",
        (
            matchup["id"],
            matchup["guild_id"],
            matchup["season"],
            opened_by,
            kind,
            reason,
            requested_deadline_at,
            iso_now(),
        ),
    )


async def latest_open_case(db: Database, matchup_id: int):
    return await db.fetchone(
        """SELECT * FROM matchup_cases WHERE matchup_id=? AND status IN ('open','processing')
           ORDER BY id DESC LIMIT 1""",
        (matchup_id,),
    )


def case_embed(case, matchup) -> discord.Embed:
    embed = discord.Embed(
        title=f"Case #{case['id']} · {case['kind'].replace('_', ' ').title()}",
        description=case["reason"],
        color=discord.Color.red()
        if case["kind"] in ("issue", "concession", "no_show")
        else discord.Color.gold(),
    )
    embed.add_field(
        name="Matchup",
        value=f"#{matchup['id']} · {matchup['away_team']} @ {matchup['home_team']}",
        inline=False,
    )
    embed.add_field(name="Opened By", value=f"<@{case['opened_by']}>")
    if case["requested_deadline_at"]:
        from datetime import datetime

        requested = datetime.fromisoformat(case["requested_deadline_at"])
        embed.add_field(
            name="Requested Deadline",
            value=f"<t:{int(requested.timestamp())}:F>",
        )
    embed.set_footer(text="Every resolution is audited and important actions confirm twice.")
    return embed


async def resolve_case(
    interaction: discord.Interaction,
    db: Database,
    case_id: int,
    action: str,
    *,
    custom_deadline: str | None = None,
) -> str:
    case = await db.fetchone(
        "SELECT * FROM matchup_cases WHERE id=?", (case_id,)
    )
    if not case or case["status"] != "open":
        raise ValueError("This case has already been resolved or is being handled.")
    if not await _claim_case(db, case_id):
        raise ValueError("Another Commissioner is already handling this case.")
    try:
        return await _resolve_claimed_case(
            interaction,
            db,
            case_id,
            action,
            custom_deadline=custom_deadline,
        )
    except BaseException:
        await db.execute(
            "UPDATE matchup_cases SET status='open' WHERE id=? AND status='processing'",
            (case_id,),
        )
        raise

async def _resolve_claimed_case(
    interaction: discord.Interaction,
    db: Database,
    case_id: int,
    action: str,
    *,
    custom_deadline: str | None = None,
) -> str:
    case = await db.fetchone(
        "SELECT * FROM matchup_cases WHERE id=?", (case_id,)
    )
    if not case or case["status"] != "processing":
        raise ValueError("This case is no longer available for resolution.")
    matchup = await db.fetchone(
        "SELECT * FROM matchups WHERE id=?", (case["matchup_id"],)
    )
    if not matchup or matchup["status"] in FINAL_STATUSES:
        await _close_case(
            db, case_id, interaction.user.id, "Matchup was already final."
        )
        raise ValueError("The matchup is already final; the case was closed.")

    if action in ("extend_24h", "custom_deadline"):
        if action == "extend_24h":
            from datetime import UTC, datetime, timedelta

            base = (
                datetime.fromisoformat(matchup["deadline_at"])
                if matchup["deadline_at"]
                else datetime.now(UTC)
            )
            deadline = (base + timedelta(hours=24)).isoformat()
        else:
            deadline = custom_deadline
        await db.execute(
            "UPDATE matchups SET deadline_at=?,updated_at=? WHERE id=?",
            (deadline, iso_now(), matchup["id"]),
        )
        resolution = f"Deadline extended to {deadline}."
    elif action == "reopen":
        await db.execute(
            """UPDATE matchups SET status='waiting',scheduled_at=NULL,
               proposed_by=NULL,proposed_at=NULL,updated_at=? WHERE id=?""",
            (iso_now(), matchup["id"]),
        )
        resolution = "Scheduling reopened and the previous accepted time was cleared."
    elif action == "concession":
        if case["kind"] != "concession":
            raise ValueError("This case is not a concession request.")
        if case["opened_by"] == matchup["away_user_id"]:
            outcome = "force_home"
        elif case["opened_by"] == matchup["home_user_id"]:
            outcome = "force_away"
        else:
            raise ValueError("The conceding member no longer owns either team.")
        from .commissioner_ui import apply_commissioner_outcome, publish_outcome

        if not await apply_commissioner_outcome(
            db, matchup, outcome, interaction.user.id
        ):
            raise ValueError("Another Commissioner already finalized this matchup.")
        await db.execute(
            "UPDATE matchups SET result_review_note=?,updated_at=? WHERE id=?",
            (
                f"Concession approved for <@{case['opened_by']}>: {case['reason']}",
                iso_now(),
                matchup["id"],
            ),
        )
        await publish_outcome(interaction.client, matchup, outcome)
        resolution = f"Concession approved; opponent received the {outcome.replace('_', ' ')}."
    elif action == "dismiss":
        resolution = "Case dismissed with no matchup change."
    else:
        raise ValueError("Unsupported case resolution.")

    await _close_case(db, case_id, interaction.user.id, resolution)
    if action in ("dismiss", "extend_24h", "custom_deadline"):
        remaining = await db.fetchone(
            """SELECT COUNT(*) AS total FROM matchup_cases
               WHERE matchup_id=? AND status IN ('open','processing')""",
            (matchup["id"],),
        )
        if matchup["status"] == "issue_reported" and not remaining["total"]:
            await db.execute(
                """UPDATE matchups SET status='waiting',updated_at=?
                   WHERE id=? AND status='issue_reported'""",
                (iso_now(), matchup["id"]),
            )
    await db.audit(
        matchup["guild_id"],
        interaction.user.id,
        f"case_{action}",
        target_type="matchup",
        target_id=str(matchup["id"]),
        details={"case_id": case_id, "resolution": resolution},
    )
    await _notify_case_resolution(interaction, matchup, case, resolution)
    return resolution


async def _claim_case(db: Database, case_id: int) -> bool:
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "UPDATE matchup_cases SET status='processing' WHERE id=? AND status='open'",
            (case_id,),
        )
        await conn.commit()
        return cursor.rowcount == 1

async def _close_case(
    db: Database, case_id: int, actor_id: int, resolution: str
) -> None:
    await db.execute(
        """UPDATE matchup_cases SET status='resolved',resolution=?,
           resolved_by=?,resolved_at=? WHERE id=? AND status IN ('open','processing')""",
        (resolution, actor_id, iso_now(), case_id),
    )


async def _notify_case(
    interaction: discord.Interaction,
    matchup,
    case_id: int,
    *,
    title: str,
    details: str,
) -> None:
    await interaction.client.db.audit(
        matchup["guild_id"],
        interaction.user.id,
        "matchup_case_opened",
        target_type="matchup",
        target_id=str(matchup["id"]),
        details={"case_id": case_id, "title": title},
    )
    settings = await interaction.client.db.settings(matchup["guild_id"])
    guild = interaction.client.get_guild(matchup["guild_id"])
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        mention = (
            f"<@&{settings['commissioner_role_id']}>"
            if settings.get("commissioner_role_id")
            else "Commissioners"
        )
        await audit.send(
            mention,
            embed=discord.Embed(
                title=title,
                description=(
                    f"**Case:** #{case_id}\n"
                    f"**Matchup:** #{matchup['id']} · "
                    f"{matchup['away_team']} @ {matchup['home_team']}\n{details}"
                ),
                color=discord.Color.gold(),
            ),
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False
            ),
        )


async def _notify_case_resolution(
    interaction: discord.Interaction,
    matchup,
    case,
    resolution: str,
) -> None:
    for user_id in {
        matchup["away_user_id"],
        matchup["home_user_id"],
        case["opened_by"],
    } - {None}:
        await _dm_user(
            interaction.client,
            user_id,
            (
                f"Commissioner resolved case #{case['id']} for **"
                f"{matchup['away_team']} @ {matchup['home_team']}**: {resolution}"
            ),
        )
    settings = await interaction.client.db.settings(matchup["guild_id"])
    guild = interaction.client.get_guild(matchup["guild_id"])
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        try:
            await audit.send(
                embed=discord.Embed(
                    title=f"Case #{case['id']} Resolved",
                    description=resolution,
                    color=discord.Color.green(),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


def _is_active_owner(matchup, user_id: int) -> bool:
    return bool(
        matchup
        and matchup["status"] not in FINAL_STATUSES
        and user_id in (matchup["away_user_id"], matchup["home_user_id"])
    )


async def _dm_user(
    client: discord.Client, user_id: int | None, content: str
) -> None:
    if not user_id:
        return
    user = client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except discord.HTTPException:
            return
    try:
        await user.send(content)
    except (discord.Forbidden, discord.HTTPException):
        pass

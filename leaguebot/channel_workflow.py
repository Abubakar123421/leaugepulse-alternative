from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import discord

from .availability_ui import create_or_update_case
from .checks import is_commissioner
from .db import Database
from .helpers import FINAL_STATUSES, iso_now, parse_user_datetime, status_label
from .team_roles import team_role
from .team_emojis import team_label


SCHEDULE = "📅"
COUNTER = "🔁"
CONFIRM = "✅"
FINISH = "🏁"
DISPUTE = "⚠️"
HOME_FORCE = "🏠"
AWAY_FORCE = "✈️"
FAIR_SIM = "🤝"
HELP = "🆘"
REACTIONS = (SCHEDULE, COUNTER, CONFIRM, FINISH, DISPUTE, HOME_FORCE, AWAY_FORCE, FAIR_SIM, HELP)


async def matchup_channel_embed(db: Database, matchup, settings: dict, guild: discord.Guild | None = None) -> discord.Embed:
    away_team = await db.fetchone(
        "SELECT wins,losses,ties,logo_url FROM teams WHERE guild_id=? AND season=? AND lower(name)=lower(?)",
        (matchup["guild_id"], matchup["season"], matchup["away_team"]),
    )
    home_team = await db.fetchone(
        "SELECT wins,losses,ties,logo_url FROM teams WHERE guild_id=? AND season=? AND lower(name)=lower(?)",
        (matchup["guild_id"], matchup["season"], matchup["home_team"]),
    )
    away_role = await db.fetchone(
        "SELECT role_id FROM team_roles WHERE guild_id=? AND lower(team_name)=lower(?)",
        (matchup["guild_id"], matchup["away_team"]),
    )
    home_role = await db.fetchone(
        "SELECT role_id FROM team_roles WHERE guild_id=? AND lower(team_name)=lower(?)",
        (matchup["guild_id"], matchup["home_team"]),
    )
    owners = (
        f"<@{matchup['away_user_id']}>" if matchup["away_user_id"] else "Unassigned",
        f"<@{matchup['home_user_id']}>" if matchup["home_user_id"] else "Unassigned",
    )
    away_display = (
        await team_label(db, guild, matchup["season"], matchup["away_team"], bold=True)
        if guild else f"**{matchup['away_team']}**"
    )
    home_display = (
        await team_label(db, guild, matchup["season"], matchup["home_team"], bold=True)
        if guild else f"**{matchup['home_team']}**"
    )
    embed = discord.Embed(
        title=f"🏈 {matchup['away_team']} @ {matchup['home_team']} — Week {matchup['week']}",
        description=(
            f"{away_display} ({owners[0]}) "
            f"{f'<@&{away_role['role_id']}>' if away_role else ''} at "
            f"{home_display} ({owners[1]}) "
            f"{f'<@&{home_role['role_id']}>' if home_role else ''}\n\n"
            "Please schedule and play your game before the advance deadline."
        ),
        color=discord.Color.blurple(),
    )
    def record(team) -> str:
        return f"{team['wins']}-{team['losses']}-{team['ties']}" if team else "0-0-0"

    embed.add_field(
        name="Team Records",
        value=f"{away_display}: **{record(away_team)}**\n{home_display}: **{record(home_team)}**",
        inline=False,
    )
    embed.add_field(
        name="Status",
        value=status_label(matchup["status"], matchup["deadline_at"]),
        inline=False,
    )
    if matchup["scheduled_at"]:
        scheduled = datetime.fromisoformat(matchup["scheduled_at"])
        embed.add_field(
            name="📅 Confirmed Game Time",
            value=f"<t:{int(scheduled.timestamp())}:F>\n{settings['timezone']}",
            inline=False,
        )
    elif matchup["proposed_at"]:
        proposed = datetime.fromisoformat(matchup["proposed_at"])
        embed.add_field(
            name="Pending Proposal",
            value=f"<t:{int(proposed.timestamp())}:F> · waiting for opponent confirmation",
            inline=False,
        )
    if matchup["away_score"] is not None:
        embed.add_field(
            name="Submitted Result",
            value=f"{away_display} **{matchup['away_score']}–{matchup['home_score']}** {home_display}",
            inline=False,
        )
    deadline = datetime.fromisoformat(matchup["deadline_at"]) if matchup["deadline_at"] else None
    embed.add_field(
        name="Advance Deadline",
        value=f"<t:{int(deadline.timestamp())}:F>" if deadline else "Not configured",
        inline=False,
    )
    embed.add_field(
        name="Reactions",
        value=(
            "📅 — Schedule game\n"
            "🔁 — Counter proposed time\n"
            "✅ — Accept or confirm\n"
            "🏁 — Submit result\n"
            "⚠️ — Dispute submitted result\n"
            "🏠 — Request home-team force win\n"
            "✈️ — Request away-team force win\n"
            "🤝 — Request fair simulation\n"
            "🆘 — Request Commissioner help"
        ),
        inline=False,
    )
    embed.set_footer(text="Only the two team owners and league management can react.")
    return embed


async def create_week_matchup_channels(
    interaction: discord.Interaction, db: Database, *, season: str, week: int
) -> tuple[int, list[str]]:
    guild = interaction.guild
    settings = await db.settings(guild.id)
    errors: list[str] = []
    category_row = await db.fetchone(
        "SELECT category_id FROM week_categories WHERE guild_id=? AND season=? AND week=?",
        (guild.id, season, week),
    )
    category = guild.get_channel(category_row["category_id"] or 0) if category_row else None
    if not isinstance(category, discord.CategoryChannel):
        try:
            category = await guild.create_category(
                f"WEEK {week} MATCHUPS",
                reason=f"Season {season} Week {week} matchup category",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return 0, [f"Could not create the Week {week} category: {exc}"]
    await db.execute(
        """INSERT INTO week_categories (guild_id,season,week,category_id,created_at)
           VALUES (?,?,?,?,?) ON CONFLICT(guild_id,season,week)
           DO UPDATE SET category_id=excluded.category_id""",
        (guild.id, season, week, category.id, iso_now()),
    )
    rows = await db.fetchall(
        "SELECT * FROM matchups WHERE guild_id=? AND season=? AND week=? ORDER BY id",
        (guild.id, season, week),
    )
    created = 0
    commissioner_role = guild.get_role(settings.get("commissioner_role_id") or 0)
    for matchup in rows:
        channel = guild.get_channel(matchup["channel_id"] or 0)
        if not isinstance(channel, discord.TextChannel):
            away_role = await team_role(db, guild, matchup["away_team"])
            home_role = await team_role(db, guild, matchup["home_team"])
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, add_reactions=False
                )
            }
            for role in (away_role, home_role, commissioner_role):
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        add_reactions=True,
                        read_message_history=True,
                        attach_files=True,
                    )
            if guild.me:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, add_reactions=True,
                    manage_messages=True, manage_channels=True, read_message_history=True,
                    attach_files=True, embed_links=True,
                )
            try:
                channel = await guild.create_text_channel(
                    _channel_name(matchup["away_team"], matchup["home_team"]),
                    category=category,
                    overwrites=overwrites,
                    topic=f"Madden {season} Week {week} · matchup #{matchup['id']}",
                    reason="Weekly Madden matchup channel",
                )
                created += 1
                await db.execute(
                    "UPDATE matchups SET channel_id=?,updated_at=? WHERE id=?",
                    (channel.id, iso_now(), matchup["id"]),
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"{matchup['away_team']} @ {matchup['home_team']}: {exc}")
                continue
        current = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup["id"],))
        await ensure_matchup_message(channel, db, current, settings)
    return created, errors


async def ensure_matchup_message(
    channel: discord.TextChannel, db: Database, matchup, settings: dict
) -> discord.Message:
    message = None
    if matchup["message_id"]:
        try:
            message = await channel.fetch_message(matchup["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    embed = await matchup_channel_embed(db, matchup, settings, channel.guild)
    if message:
        await message.edit(embed=embed, view=None)
    else:
        mentions = " ".join(
            f"<@{uid}>" for uid in (matchup["away_user_id"], matchup["home_user_id"]) if uid
        )
        message = await channel.send(
            f"{mentions} Please schedule this matchup as soon as possible.",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        for emoji in REACTIONS:
            await message.add_reaction(emoji)
        await db.execute(
            "UPDATE matchups SET message_id=?,channel_id=?,updated_at=? WHERE id=?",
            (message.id, channel.id, iso_now(), matchup["id"]),
        )
    return message


async def refresh_matchup_message(client: discord.Client, db: Database, matchup_id: int) -> None:
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    if not matchup or not matchup["channel_id"]:
        return
    channel = client.get_channel(matchup["channel_id"])
    if isinstance(channel, discord.TextChannel):
        await ensure_matchup_message(
            channel, db, matchup, await db.settings(matchup["guild_id"])
        )


def _valid_action_message(status: str, emoji: str, content: str) -> bool:
    normalized = content.casefold()
    return (
        status == "schedule_pending"
        and emoji in (CONFIRM, COUNTER)
        and " proposed " in f" {normalized} "
    ) or (
        status == "result_pending"
        and emoji in (CONFIRM, DISPUTE)
        and "submitted with private evidence" in normalized
    )

async def handle_raw_reaction(client: discord.Client, db: Database, payload) -> None:
    if payload.guild_id is None or client.user and payload.user_id == client.user.id:
        return
    emoji = str(payload.emoji)
    channel = client.get_channel(payload.channel_id)
    matchup = await db.fetchone(
        "SELECT * FROM matchups WHERE guild_id=? AND channel_id=? AND message_id=?",
        (payload.guild_id, payload.channel_id, payload.message_id),
    )
    if not matchup and isinstance(channel, discord.TextChannel):
        candidate = await db.fetchone(
            "SELECT * FROM matchups WHERE guild_id=? AND channel_id=?",
            (payload.guild_id, payload.channel_id),
        )
        valid_action = bool(candidate) and (
            (candidate["status"] == "schedule_pending" and emoji in (CONFIRM, COUNTER))
            or (candidate["status"] == "result_pending" and emoji in (CONFIRM, DISPUTE))
        )
        if valid_action:
            try:
                action_message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                action_message = None
            content = action_message.content.casefold() if action_message else ""
            is_bot_message = bool(
                action_message and client.user and action_message.author.id == client.user.id
            )
            is_expected_message = _valid_action_message(
                candidate["status"], emoji, content
            )
            if is_bot_message and is_expected_message:
                matchup = candidate
    if not matchup:
        return
    guild = client.get_guild(payload.guild_id)
    member = payload.member or (guild.get_member(payload.user_id) if guild else None)
    settings = await db.settings(payload.guild_id)
    allowed = payload.user_id in (matchup["away_user_id"], matchup["home_user_id"])
    if member and not allowed:
        role_id = settings.get("commissioner_role_id")
        allowed = member.guild_permissions.administrator or bool(
            role_id and member.get_role(role_id)
        )
    if not allowed or emoji not in REACTIONS or matchup["status"] in FINAL_STATUSES:
        await _remove_reaction(channel, payload.message_id, payload.emoji, member)
        return
    await _remove_reaction(channel, payload.message_id, payload.emoji, member)
    owner_only = (SCHEDULE, COUNTER, CONFIRM, FINISH, DISPUTE, HOME_FORCE, AWAY_FORCE)
    if emoji in owner_only and payload.user_id not in (
        matchup["away_user_id"], matchup["home_user_id"]
    ):
        await channel.send("Commissioners should use `/week` for this decision.")
        return
    if emoji in (SCHEDULE, COUNTER):
        if emoji == SCHEDULE and matchup["status"] == "schedule_pending":
            await channel.send(
                f"<@{payload.user_id}> a proposal is already pending. "
                "The opposing owner can react 🔁 to counter it."
            )
            return
        if emoji == COUNTER and matchup["status"] != "schedule_pending":
            await channel.send(f"<@{payload.user_id}> there is no pending time to counter.")
            return
        if emoji == COUNTER and matchup["proposed_by"] in (None, payload.user_id):
            await channel.send(f"<@{payload.user_id}> only the opposing owner can counter the current proposal.")
            return
        await _set_prompt(db, matchup["id"], payload.user_id, "schedule")
        await channel.send(
            f"<@{payload.user_id}> type the proposed date and time in `{settings['timezone']}` "
            "using `YYYY-MM-DD HH:MM` within 10 minutes."
        )
    elif emoji == CONFIRM:
        await _confirm_reaction(client, db, channel, matchup, payload.user_id)
    elif emoji == FINISH:
        await _set_prompt(db, matchup["id"], payload.user_id, "result")
        await channel.send(
            f"<@{payload.user_id}> type the score as `away-home` (example `24-17`) "
            "and attach one PNG/JPG/WebP final-score screenshot within 10 minutes."
        )
    elif emoji == DISPUTE:
        if matchup["status"] != "result_pending" or payload.user_id == matchup["result_submitted_by"]:
            await channel.send(f"<@{payload.user_id}> there is no opponent result for you to dispute.")
            return
        await db.execute(
            """UPDATE matchups SET status='issue_reported',result_opponent_status='disputed',
               result_opponent_by=?,updated_at=? WHERE id=? AND status='result_pending'""",
            (payload.user_id, iso_now(), matchup["id"]),
        )
        await create_or_update_case(
            db, matchup=matchup, opened_by=payload.user_id, kind="result_dispute",
            reason="Opponent disputed the submitted result from the matchup reaction.",
        )
        await channel.send("⚠️ Result disputed. Commissioners were notified privately.")
        await refresh_matchup_message(client, db, matchup["id"])
    else:
        await _open_reaction_case(client, db, channel, matchup, payload.user_id, emoji)


async def handle_matchup_message(client: discord.Client, db: Database, message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    matchup = await db.fetchone(
        "SELECT * FROM matchups WHERE guild_id=? AND channel_id=?",
        (message.guild.id, message.channel.id),
    )
    if not matchup:
        return
    prompt = await db.fetchone(
        """SELECT * FROM matchup_prompts WHERE matchup_id=? AND user_id=?""",
        (matchup["id"], message.author.id),
    )
    if not prompt:
        return
    if datetime.fromisoformat(prompt["expires_at"]) < datetime.now(UTC):
        await db.execute(
            "DELETE FROM matchup_prompts WHERE matchup_id=? AND user_id=?",
            (matchup["id"], message.author.id),
        )
        await message.reply("That request expired. React again to restart it.")
        return
    settings = await db.settings(message.guild.id)
    if prompt["kind"] == "schedule":
        try:
            proposed = parse_user_datetime(message.content.strip(), settings["timezone"])
            if proposed <= datetime.now(UTC):
                raise ValueError("The proposed time must be in the future.")
            if matchup["deadline_at"] and proposed > datetime.fromisoformat(matchup["deadline_at"]):
                raise ValueError("That time is after the advance deadline.")
        except ValueError as exc:
            await message.reply(str(exc))
            return
        await db.execute(
            """UPDATE matchups SET status='schedule_pending',proposed_by=?,proposed_at=?,
               schedule_proposal_version=schedule_proposal_version+1,updated_at=? WHERE id=?""",
            (message.author.id, proposed.isoformat(), iso_now(), matchup["id"]),
        )
        await db.audit(
            matchup["guild_id"], message.author.id, "schedule_proposed",
            target_type="matchup", target_id=str(matchup["id"]),
            details={"proposed_at": proposed.isoformat()},
        )
        await _send_schedule_audit(
            client, db, matchup, message.author.id, "Schedule proposed",
            f"Proposed time: <t:{int(proposed.timestamp())}:F>",
        )
        action_message = await message.channel.send(
            f"📅 <@{message.author.id}> proposed <t:{int(proposed.timestamp())}:F>. "
            "The opposing owner should react ✅ to accept or 🔁 to counter on this message."
        )
        for action_emoji in (CONFIRM, COUNTER):
            await action_message.add_reaction(action_emoji)
    else:
        score = re.fullmatch(r"\s*(\d{1,3})\s*[-:]\s*(\d{1,3})\s*", message.content)
        attachment = message.attachments[0] if message.attachments else None
        if not message.content.strip():
            await message.reply(
                "I received the screenshot but Discord did not provide its score text. "
                "React 🏁 again, then send `away-home` and the image together."
            )
            return
        if not score:
            await message.reply(
                f"I read `{message.content[:40]}`. Enter only `away-home`, for example `24-17`."
            )
            return
        if not attachment:
            await message.reply(
                "The score is valid, but no screenshot was attached to the same message."
            )
            return
        if attachment.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            await message.reply(
                f"The score is valid, but `{attachment.filename}` is not a PNG, JPG, or WebP image."
            )
            return
        from .result_ui import MAX_EVIDENCE_BYTES, submit_result_from_message

        if attachment.size > MAX_EVIDENCE_BYTES:
            await message.reply("That screenshot is larger than 8 MB.")
            return
        away, home = map(int, score.groups())
        if away == home:
            await message.reply("League games cannot end tied.")
            return
        audit = message.guild.get_channel(settings.get("audit_channel_id") or 0)
        if not isinstance(audit, discord.TextChannel):
            await message.reply("The commissioner audit channel is missing. Run `/setup`.")
            return
        submitted, notice = await submit_result_from_message(
            client,
            db,
            matchup,
            settings,
            message.author.id,
            away,
            home,
            attachment,
        )
        if not submitted:
            await message.reply(notice)
            return
        action_message = await message.channel.send(
            f"🏁 Score **{away}-{home}** submitted with private evidence. "
            "The opposing owner should react ✅ to confirm or ⚠️ to dispute on this message."
        )
        for action_emoji in (CONFIRM, DISPUTE):
            await action_message.add_reaction(action_emoji)
    await db.execute(
        "DELETE FROM matchup_prompts WHERE matchup_id=? AND user_id=?",
        (matchup["id"], message.author.id),
    )
    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    await refresh_matchup_message(client, db, matchup["id"])


async def lock_matchup_channel(
    db: Database, guild: discord.Guild, matchup
) -> bool:
    channel = guild.get_channel(matchup["channel_id"] or 0)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        for team_name in (matchup["away_team"], matchup["home_team"]):
            role = await team_role(db, guild, team_name)
            if role:
                await channel.set_permissions(
                    role,
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False,
                    read_message_history=True,
                )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def lock_and_delete_week_channels(
    guild: discord.Guild, db: Database, season: str, week: int
) -> list[str]:
    errors: list[str] = []
    settings = await db.settings(guild.id)
    rows = await db.fetchall(
        "SELECT id,channel_id FROM matchups WHERE guild_id=? AND season=? AND week=?",
        (guild.id, season, week),
    )
    for row in rows:
        channel = guild.get_channel(row["channel_id"] or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.delete(reason=f"Madden {season} Week {week} completed")
                await db.execute(
                    "UPDATE matchups SET channel_id=NULL,message_id=NULL,updated_at=? WHERE id=?",
                    (iso_now(), row["id"]),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"{channel.name}: {exc}")
    category_row = await db.fetchone(
        "SELECT category_id FROM week_categories WHERE guild_id=? AND season=? AND week=?",
        (guild.id, season, week),
    )
    category = guild.get_channel(category_row["category_id"] or 0) if category_row else None
    if not errors and isinstance(category, discord.CategoryChannel):
        try:
            await category.delete(reason=f"Madden {season} Week {week} completed")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"category: {exc}")
    if not errors:
        await db.execute(
            "DELETE FROM week_categories WHERE guild_id=? AND season=? AND week=?",
            (guild.id, season, week),
        )
    return errors


async def _confirm_reaction(client, db, channel, matchup, user_id: int) -> None:
    if matchup["status"] == "schedule_pending" and user_id != matchup["proposed_by"]:
        await db.execute(
            """UPDATE matchups SET status='scheduled',scheduled_at=proposed_at,
               schedule_previous_at=NULL,updated_at=? WHERE id=? AND status='schedule_pending'""",
            (iso_now(), matchup["id"]),
        )
        await channel.send(f"✅ <@{user_id}> accepted the game time. Scheduling reminders stopped.")
        accepted = matchup["proposed_at"]
        await db.audit(
            matchup["guild_id"], user_id, "schedule_confirmed",
            target_type="matchup", target_id=str(matchup["id"]),
            details={"scheduled_at": accepted},
        )
        await _send_schedule_audit(
            client, db, matchup, user_id, "Schedule confirmed",
            f"Confirmed time: <t:{int(datetime.fromisoformat(accepted).timestamp())}:F>",
        )
    elif matchup["status"] in ("result_pending", "issue_reported") and user_id != matchup["result_submitted_by"]:
        await db.execute(
            """UPDATE matchups SET status='result_pending',result_opponent_status='confirmed',
               result_opponent_by=?,updated_at=? WHERE id=?""",
            (user_id, iso_now(), matchup["id"]),
        )
        await db.audit(
            matchup["guild_id"],
            user_id,
            "result_confirmed",
            target_type="matchup",
            target_id=str(matchup["id"]),
            details={"submission_version": matchup["result_submission_version"]},
        )
        updated = await db.fetchone(
            "SELECT * FROM matchups WHERE id=?", (matchup["id"],)
        )
        from .result_ui import _refresh_audit_message

        await _refresh_audit_message(client, updated)
        await channel.send("✅ The opponent confirmed the result. Commissioner approval is pending.")
    else:
        await channel.send(f"<@{user_id}> there is nothing awaiting your confirmation.")
        return
    await refresh_matchup_message(client, db, matchup["id"])


async def _open_reaction_case(client, db, channel, matchup, user_id: int, emoji: str) -> None:
    kind, reason = {
        HOME_FORCE: ("force_home_request", "Home team requested a force win."),
        AWAY_FORCE: ("force_away_request", "Away team requested a force win."),
        FAIR_SIM: ("fair_sim_request", "An owner requested a fair simulation."),
        HELP: ("commissioner_help", "An owner requested commissioner assistance."),
    }[emoji]
    if emoji == HOME_FORCE and user_id != matchup["home_user_id"]:
        await channel.send("Only the home-team owner can request the home force win.")
        return
    if emoji == AWAY_FORCE and user_id != matchup["away_user_id"]:
        await channel.send("Only the away-team owner can request the away force win.")
        return
    case_id = await create_or_update_case(
        db, matchup=matchup, opened_by=user_id, kind=kind, reason=reason
    )
    settings = await db.settings(matchup["guild_id"])
    guild = client.get_guild(matchup["guild_id"])
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        await audit.send(
            f"<@&{settings['commissioner_role_id']}> **Case #{case_id}** · {reason}\n"
            f"Matchup: <#{matchup['channel_id']}> · requested by <@{user_id}>"
        )
    await channel.send(f"{emoji} Request recorded privately for the commissioners.")


async def _send_schedule_audit(
    client: discord.Client,
    db: Database,
    matchup,
    actor_id: int,
    title: str,
    detail: str,
) -> None:
    settings = await db.settings(matchup["guild_id"])
    guild = client.get_guild(matchup["guild_id"])
    audit = guild.get_channel(settings.get("audit_channel_id") or 0) if guild else None
    if isinstance(audit, discord.TextChannel):
        try:
            await audit.send(
                embed=discord.Embed(
                    title=title,
                    description=(
                        f"**Matchup:** <#{matchup['channel_id']}> · "
                        f"{matchup['away_team']} @ {matchup['home_team']}\n"
                        f"**Player:** <@{actor_id}>\n{detail}"
                    ),
                    color=discord.Color.blurple(),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _set_prompt(db: Database, matchup_id: int, user_id: int, kind: str) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=10)
    await db.execute(
        """INSERT INTO matchup_prompts (matchup_id,user_id,kind,expires_at,created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(matchup_id,user_id) DO UPDATE SET
           kind=excluded.kind,expires_at=excluded.expires_at,created_at=excluded.created_at""",
        (matchup_id, user_id, kind, expires.isoformat(), iso_now()),
    )


async def _remove_reaction(channel, message_id, emoji, member) -> None:
    if not isinstance(channel, discord.TextChannel) or member is None:
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.remove_reaction(emoji, member)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def _channel_name(away: str, home: str) -> str:
    raw = f"{away}-at-{home}".lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", raw)[:100]

"""One-time clean reset for the current unlaunched Madden season."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord

from leaguebot.channel_workflow import create_week_matchup_channels
from leaguebot.config import Config
from leaguebot.db import Database
from leaguebot.helpers import iso_now, utcnow
from leaguebot.open_teams_ui import refresh_open_teams_panel
from leaguebot.services import make_backup
from leaguebot.team_emojis import sync_team_emojis
from leaguebot.team_roles import clear_team_role_members


class PrelaunchResetClient(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.config = config
        self.db = Database(config.database_path)
        self.completed = False

    async def setup_hook(self) -> None:
        await self.db.initialize()

    async def on_ready(self) -> None:
        if self.completed:
            return
        self.completed = True
        try:
            for guild in self.guilds:
                await self.reset_guild(guild)
        finally:
            await self.close()

    async def reset_guild(self, guild: discord.Guild) -> None:
        settings = await self.db.settings(guild.id)
        season = settings["season"]
        archived = await self.db.fetchone(
            "SELECT COUNT(*) AS total FROM season_archives WHERE guild_id=?",
            (guild.id,),
        )
        history = await self.db.fetchone(
            "SELECT COUNT(*) AS total FROM game_history WHERE guild_id=?",
            (guild.id,),
        )
        if (archived and archived["total"]) or (history and history["total"]):
            raise RuntimeError(
                "Permanent history exists; refusing to run the unlaunched-season reset."
            )

        backup = await make_backup(self.db, self.config.backup_dir)
        print(f"Backup created: {backup}")

        # Remove evidence/review messages whose attachments belong only to test results.
        audit = guild.get_channel(settings.get("audit_channel_id") or 0)
        audit_ids = await self.db.fetchall(
            """SELECT result_audit_message_id FROM matchups
               WHERE guild_id=? AND season=? AND result_audit_message_id IS NOT NULL""",
            (guild.id, season),
        )
        if isinstance(audit, discord.TextChannel):
            for row in audit_ids:
                try:
                    await audit.get_partial_message(
                        row["result_audit_message_id"]
                    ).delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        # Remove test weekly channels before clearing their IDs.
        channel_rows = await self.db.fetchall(
            """SELECT DISTINCT channel_id FROM matchups
               WHERE guild_id=? AND season=? AND channel_id IS NOT NULL""",
            (guild.id, season),
        )
        for row in channel_rows:
            channel = guild.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.delete(reason="Pre-launch Madden test reset")
                except discord.NotFound:
                    pass
        category_rows = await self.db.fetchall(
            """SELECT category_id FROM week_categories
               WHERE guild_id=? AND season=?""",
            (guild.id, season),
        )
        for row in category_rows:
            category = guild.get_channel(row["category_id"])
            if isinstance(category, discord.CategoryChannel):
                try:
                    await category.delete(reason="Pre-launch Madden test reset")
                except discord.NotFound:
                    pass

        role_errors = await clear_team_role_members(guild, self.db)
        now = utcnow()
        first_deadline = now + timedelta(days=7)
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                """DELETE FROM matchup_prompts WHERE matchup_id IN
                   (SELECT id FROM matchups WHERE guild_id=? AND season=?)""",
                (guild.id, season),
            )
            await conn.execute(
                """DELETE FROM reminder_deliveries WHERE matchup_id IN
                   (SELECT id FROM matchups WHERE guild_id=? AND season=?)""",
                (guild.id, season),
            )
            await conn.execute(
                "DELETE FROM matchup_cases WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute(
                """UPDATE matchups SET
                   away_user_id=NULL,home_user_id=NULL,away_score=NULL,home_score=NULL,
                   proposed_by=NULL,proposed_at=NULL,scheduled_at=NULL,
                   schedule_previous_at=NULL,schedule_proposal_version=0,
                   status='waiting',issue_text=NULL,thread_id=NULL,channel_id=NULL,
                   message_id=NULL,commissioner_pinged_at=NULL,
                   result_submission_version=0,result_submitted_by=NULL,
                   result_submitted_at=NULL,result_evidence_url=NULL,
                   result_opponent_status=NULL,result_opponent_by=NULL,
                   result_audit_message_id=NULL,result_reviewed_by=NULL,
                   result_reviewed_at=NULL,result_review_note=NULL,
                   final_score_message_id=NULL,final_score_posted_at=NULL,
                   deadline_at=?,updated_at=?
                   WHERE guild_id=? AND season=?""",
                (first_deadline.isoformat(), iso_now(), guild.id, season),
            )
            # Give each future week its own seven-day deadline.
            rows = await conn.execute(
                "SELECT id,week FROM matchups WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            for matchup in await rows.fetchall():
                deadline = now + timedelta(days=7 * matchup["week"])
                await conn.execute(
                    "UPDATE matchups SET deadline_at=? WHERE id=?",
                    (deadline.isoformat(), matchup["id"]),
                )
            await conn.execute("DELETE FROM profiles WHERE guild_id=?", (guild.id,))
            await conn.execute(
                "DELETE FROM season_participants WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute(
                "DELETE FROM career_events WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute("DELETE FROM career_profiles WHERE guild_id=?", (guild.id,))
            await conn.execute(
                """UPDATE teams SET wins=0,losses=0,ties=0
                   WHERE guild_id=? AND season=?""",
                (guild.id, season),
            )
            await conn.execute(
                "DELETE FROM open_rosters WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute(
                """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                   SELECT guild_id,season,team_name,'Available for launch',?
                   FROM franchises WHERE guild_id=? AND season=?""",
                (iso_now(), guild.id, season),
            )
            for table in (
                "week_rollovers", "weekly_mvps", "weekly_recaps",
                "game_of_week_posts", "season_awards", "season_award_summaries",
            ):
                await conn.execute(
                    f"DELETE FROM {table} WHERE guild_id=? AND season=?",
                    (guild.id, season),
                )
            await conn.execute(
                "DELETE FROM ai_jobs WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute(
                "DELETE FROM week_categories WHERE guild_id=? AND season=?",
                (guild.id, season),
            )
            await conn.execute("DELETE FROM audit_logs WHERE guild_id=?", (guild.id,))
            await conn.execute(
                """UPDATE guild_settings SET current_week=1,auto_week_rollover=1,
                   week_started_at=?,week_deadline_at=?,season_started_at=?,
                   updated_at=? WHERE guild_id=?""",
                (
                    now.isoformat(), first_deadline.isoformat(), now.isoformat(),
                    iso_now(), guild.id,
                ),
            )
            await conn.commit()

        await sync_team_emojis(self.db, guild, season)
        created, channel_errors = await create_week_matchup_channels(
            SimpleNamespace(guild=guild, client=self),
            self.db,
            season=season,
            week=1,
        )
        await refresh_open_teams_panel(self, self.db, guild.id)
        await self.db.audit(
            guild.id,
            self.user.id if self.user else 0,
            "prelaunch_full_reset",
            details={
                "season": season,
                "week_1_channels_created": created,
                "role_errors": role_errors,
                "channel_errors": channel_errors,
            },
        )
        print(
            f"Guild {guild.id}: reset complete; {created} Week 1 channels created; "
            f"{len(role_errors)} role error(s); {len(channel_errors)} channel error(s)."
        )


def main() -> None:
    config = Config.from_env()
    client = PrelaunchResetClient(config)
    client.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()

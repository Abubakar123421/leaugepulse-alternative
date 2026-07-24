from __future__ import annotations

from .db import Database
from .helpers import FINAL_STATUSES, iso_now
from .progression import ensure_participant, record_matchup_progress


async def backfill_active_leagues(db: Database) -> None:
    """Make pre-feature active data usable without changing official standings."""
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        # A process can stop after claiming a case but before completing it.
        # On restart, no worker still owns that claim, so make it actionable again.
        await conn.execute(
            "UPDATE matchup_cases SET status='open' WHERE status='processing'"
        )

        cursor = await conn.execute(
            """SELECT p.guild_id,p.user_id,p.team_name,g.season
               FROM profiles p
               JOIN guild_settings g ON g.guild_id=p.guild_id
               WHERE p.approved=1"""
        )
        for profile in await cursor.fetchall():
            await ensure_participant(
                conn,
                guild_id=profile["guild_id"],
                season=profile["season"],
                user_id=profile["user_id"],
                team_name=profile["team_name"],
            )

        cursor = await conn.execute(
            """SELECT m.* FROM matchups m
               JOIN guild_settings g
                 ON g.guild_id=m.guild_id AND g.season=m.season
               WHERE m.status IN ('complete','force_home','force_away','fair_sim')"""
        )
        for matchup in await cursor.fetchall():
            await record_matchup_progress(conn, matchup, matchup["status"])

        cursor = await conn.execute(
            """SELECT m.* FROM matchups m
               WHERE m.status='issue_reported'
                 AND NOT EXISTS (
                   SELECT 1 FROM matchup_cases c
                   WHERE c.matchup_id=m.id AND c.status='open'
                 )"""
        )
        for matchup in await cursor.fetchall():
            opened_by = (
                matchup["result_submitted_by"]
                or matchup["proposed_by"]
                or matchup["away_user_id"]
                or matchup["home_user_id"]
            )
            if not opened_by:
                audit_cursor = await conn.execute(
                    """SELECT actor_id FROM audit_logs
                       WHERE guild_id=? AND target_type='matchup' AND target_id=?
                       ORDER BY id DESC LIMIT 1""",
                    (matchup["guild_id"], str(matchup["id"])),
                )
                audit_row = await audit_cursor.fetchone()
                opened_by = audit_row["actor_id"] if audit_row else None
            if not opened_by:
                continue
            await conn.execute(
                """INSERT INTO matchup_cases
                   (matchup_id,guild_id,season,opened_by,kind,reason,status,created_at)
                   VALUES (?,?,?,?,?,?,'open',?)""",
                (
                    matchup["id"],
                    matchup["guild_id"],
                    matchup["season"],
                    opened_by,
                    "issue",
                    matchup["issue_text"] or "Legacy issue awaiting Commissioner review.",
                    iso_now(),
                ),
            )
        await conn.commit()

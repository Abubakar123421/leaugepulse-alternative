from __future__ import annotations

from dataclasses import dataclass

import discord

from .db import Database
from .awards import awards_ready
from .helpers import FINAL_STATUSES, iso_now
from .team_roles import clear_team_role_members
from .progression import (
    award_championship,
    ensure_participant,
    record_matchup_progress,
)


@dataclass(frozen=True, slots=True)
class SeasonClosePreview:
    guild_id: int
    season: str
    total_games: int
    final_games: int
    unresolved: tuple[str, ...]
    participants: int
    already_archived: bool

    @property
    def can_close(self) -> bool:
        return (
            self.total_games > 0
            and not self.unresolved
            and not self.already_archived
        )


@dataclass(frozen=True, slots=True)
class SeasonArchiveResult:
    season: str
    new_season: str
    games_archived: int
    participants_preserved: int
    champion_user_id: int | None
    champion_team: str | None


async def season_close_preview(
    db: Database, guild_id: int, season: str
) -> SeasonClosePreview:
    rows = await db.fetchall(
        """SELECT id,away_team,home_team,status FROM matchups
           WHERE guild_id=? AND season=? ORDER BY week,id""",
        (guild_id, season),
    )
    unresolved = tuple(
        f"#{row['id']} {row['away_team']} @ {row['home_team']} ({row['status']})"
        for row in rows
        if row["status"] not in FINAL_STATUSES
    )
    participants = await db.fetchone(
        """SELECT COUNT(DISTINCT user_id) AS total FROM (
             SELECT away_user_id AS user_id FROM matchups
              WHERE guild_id=? AND season=? AND away_user_id IS NOT NULL
             UNION
             SELECT home_user_id AS user_id FROM matchups
              WHERE guild_id=? AND season=? AND home_user_id IS NOT NULL
             UNION
             SELECT user_id FROM profiles WHERE guild_id=? AND approved=1
           )""",
        (guild_id, season, guild_id, season, guild_id),
    )
    archived = await db.fetchone(
        "SELECT 1 FROM season_archives WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    return SeasonClosePreview(
        guild_id=guild_id,
        season=season,
        total_games=len(rows),
        final_games=sum(row["status"] in FINAL_STATUSES for row in rows),
        unresolved=unresolved,
        participants=participants["total"] if participants else 0,
        already_archived=bool(archived),
    )


async def archive_season(
    db: Database,
    *,
    guild_id: int,
    season: str,
    new_season: str,
    actor_id: int,
    champion_user_id: int | None = None,
) -> SeasonArchiveResult:
    clean_new_season = " ".join(new_season.split())
    if not clean_new_season:
        raise ValueError("The new season name cannot be empty.")
    if clean_new_season.casefold() == season.casefold():
        raise ValueError("The new season must have a different name.")

    settings = await db.settings(guild_id)
    preview = await season_close_preview(db, guild_id, season)
    if preview.already_archived:
        raise ValueError("This season has already been archived.")
    if not preview.total_games:
        raise ValueError("Import and complete at least one game before closing a season.")
    if preview.unresolved:
        raise ValueError(
            f"{len(preview.unresolved)} matchup(s) are still unresolved."
        )
    if not await awards_ready(db, guild_id, season):
        raise ValueError(
            "Complete, approve, and publish all eight season awards before closing the season."
        )
    existing_new = await db.fetchone(
        """SELECT 1 FROM season_archives WHERE guild_id=? AND season=?
           UNION SELECT 1 FROM matchups WHERE guild_id=? AND season=? LIMIT 1""",
        (guild_id, clean_new_season, guild_id, clean_new_season),
    )
    if existing_new:
        raise ValueError("That new season name already exists in this server's history.")

    champion_team: str | None = None
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            """SELECT * FROM matchups WHERE guild_id=? AND season=?
               ORDER BY week,id""",
            (guild_id, season),
        )
        matchups = list(await cursor.fetchall())
        cursor = await conn.execute(
            "SELECT 1 FROM season_archives WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        if await cursor.fetchone():
            await conn.rollback()
            raise ValueError("This season has already been archived.")
        unresolved_now = [
            row for row in matchups if row["status"] not in FINAL_STATUSES
        ]
        if not matchups or unresolved_now:
            await conn.rollback()
            raise ValueError(
                "Season state changed before confirmation; review unresolved matchups again."
            )

        cursor = await conn.execute(
            """SELECT user_id,team_name FROM profiles
               WHERE guild_id=? AND approved=1""",
            (guild_id,),
        )
        profiles = list(await cursor.fetchall())
        for profile in profiles:
            await ensure_participant(
                conn,
                guild_id=guild_id,
                season=season,
                user_id=profile["user_id"],
                team_name=profile["team_name"],
            )

        for matchup in matchups:
            await record_matchup_progress(conn, matchup, matchup["status"])
            winner_user_id = _winner_user_id(matchup)
            await conn.execute(
                """INSERT INTO game_history
                   (guild_id,season,week,external_key,away_team,home_team,
                    away_user_id,home_user_id,away_score,home_score,status,
                    winner_user_id,decision_type,completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(guild_id,season,week,external_key) DO NOTHING""",
                (
                    guild_id,
                    season,
                    matchup["week"],
                    matchup["external_key"],
                    matchup["away_team"],
                    matchup["home_team"],
                    matchup["away_user_id"],
                    matchup["home_user_id"],
                    matchup["away_score"],
                    matchup["home_score"],
                    matchup["status"],
                    winner_user_id,
                    matchup["status"],
                    matchup["result_reviewed_at"] or matchup["updated_at"],
                ),
            )

        if champion_user_id:
            cursor = await conn.execute(
                """SELECT team_name FROM season_participants
                   WHERE guild_id=? AND season=? AND user_id=?
                   ORDER BY wins DESC,games DESC LIMIT 1""",
                (guild_id, season, champion_user_id),
            )
            champion = await cursor.fetchone()
            if not champion:
                raise ValueError(
                    "The selected champion did not participate in this season."
                )
            champion_team = champion["team_name"]
            await award_championship(
                conn,
                guild_id=guild_id,
                season=season,
                user_id=champion_user_id,
                team_name=champion_team,
            )

        await conn.execute(
            """INSERT INTO season_archives
               (guild_id,season,league_name,game,champion_user_id,champion_team,
                total_games,cleanup_status,old_category_id,archived_by,archived_at)
               VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
            (
                guild_id,
                season,
                settings["league_name"],
                settings["game"],
                champion_user_id,
                champion_team,
                len(matchups),
                settings.get("category_id"),
                actor_id,
                now,
            ),
        )

        # Audit messages are operational season noise. The archive row itself keeps
        # who closed the season and when, while official games and ownership live in
        # compact permanent tables.
        await conn.execute("DELETE FROM audit_logs WHERE guild_id=?", (guild_id,))
        await conn.execute(
            "DELETE FROM matchups WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        await conn.execute("DELETE FROM teams WHERE guild_id=? AND season=?", (guild_id, season))
        await conn.execute("DELETE FROM profiles WHERE guild_id=?", (guild_id,))
        await conn.execute(
            "DELETE FROM transactions WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        await conn.execute(
            "DELETE FROM trade_block WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        await conn.execute(
            "DELETE FROM open_rosters WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        await conn.execute(
            """UPDATE guild_settings SET season=?,current_week=1,updated_at=?
               WHERE guild_id=?""",
            (clean_new_season, now, guild_id),
        )
        await conn.commit()

    compacted = await db.compact()
    await db.execute(
        "UPDATE season_archives SET db_compacted=? WHERE guild_id=? AND season=?",
        (int(compacted), guild_id, season),
    )
    participants = await db.fetchone(
        """SELECT COUNT(DISTINCT user_id) AS total FROM season_participants
           WHERE guild_id=? AND season=?""",
        (guild_id, season),
    )
    return SeasonArchiveResult(
        season=season,
        new_season=clean_new_season,
        games_archived=preview.total_games,
        participants_preserved=participants["total"] if participants else 0,
        champion_user_id=champion_user_id,
        champion_team=champion_team,
    )


async def rotate_discord_season_space(
    guild: discord.Guild,
    db: Database,
    *,
    archived_season: str,
    new_season: str,
) -> tuple[bool, str | None]:
    """Clear season-owned matchup channels while preserving configured destinations."""
    settings = await db.settings(guild.id)
    week_rows = await db.fetchall(
        """SELECT DISTINCT category_id FROM week_categories
           WHERE guild_id=? AND season=?""",
        (guild.id, archived_season),
    )
    cleanup_errors = await clear_team_role_members(guild, db)
    configured_matchup_category = settings.get("matchup_category_id")

    for week_row in week_rows:
        category = guild.get_channel(week_row["category_id"] or 0)
        if not isinstance(category, discord.CategoryChannel):
            continue
        for channel in list(category.channels):
            try:
                await channel.delete(
                    reason=f"Madden Season {archived_season} archived"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                cleanup_errors.append(f"{channel.name}: {exc}")
        if category.id != configured_matchup_category:
            try:
                await category.delete(
                    reason=f"Legacy Madden Season {archived_season} category archived"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                cleanup_errors.append(f"{category.name}: {exc}")

    if not cleanup_errors:
        await db.execute(
            "DELETE FROM week_categories WHERE guild_id=? AND season=?",
            (guild.id, archived_season),
        )
    status = "complete" if not cleanup_errors else "partial"
    error = "; ".join(cleanup_errors)[:1000] if cleanup_errors else None
    await _set_cleanup_state(db, guild.id, archived_season, status, error)
    return not cleanup_errors, error


async def resume_season_cleanup(
    guild: discord.Guild,
    db: Database,
    *,
    archived_season: str,
) -> tuple[bool, str | None]:
    archive = await db.fetchone(
        "SELECT * FROM season_archives WHERE guild_id=? AND season=?",
        (guild.id, archived_season),
    )
    if not archive:
        return False, "That archived season does not exist."
    if archive["cleanup_status"] == "complete":
        return True, None
    settings = await db.settings(guild.id)
    return await rotate_discord_season_space(
        guild,
        db,
        archived_season=archived_season,
        new_season=settings["season"],
    )


async def _best_effort_delete_category(category: discord.CategoryChannel) -> None:
    for channel in list(category.channels):
        try:
            await channel.delete(reason="Rolling back incomplete season setup")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        await category.delete(reason="Rolling back incomplete season setup")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def _set_cleanup_state(
    db: Database,
    guild_id: int,
    season: str,
    status: str,
    error: str | None,
) -> None:
    await db.execute(
        """UPDATE season_archives SET cleanup_status=?,cleanup_error=?
           WHERE guild_id=? AND season=?""",
        (status, error, guild_id, season),
    )


def _winner_user_id(matchup) -> int | None:
    if matchup["status"] == "complete":
        if matchup["away_score"] is None or matchup["home_score"] is None:
            return None
        return (
            matchup["away_user_id"]
            if matchup["away_score"] > matchup["home_score"]
            else matchup["home_user_id"]
        )
    if matchup["status"] == "force_away":
        return matchup["away_user_id"]
    if matchup["status"] == "force_home":
        return matchup["home_user_id"]
    return None

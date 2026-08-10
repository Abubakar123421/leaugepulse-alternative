from __future__ import annotations

from dataclasses import dataclass

import discord

from .db import Database
from .helpers import iso_now
from .progression import ensure_participant
from .registration import normalize_team_name
from .team_roles import MADDEN_TEAMS, active_team_names, assign_team_role


class OwnershipError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Assignment:
    user_id: int
    team_name: str
    twitch: str | None = None
    youtube: str | None = None
    source: str = "commissioner"
    external_team_id: str | None = None
    previous_user_id: int | None = None


async def request_team_claim(
    db: Database, guild_id: int, season: str, user_id: int, team_name: str
) -> Assignment:
    """Reserve an open team as a pending claim, without assigning ownership yet."""
    resolved = await resolve_team(db, guild_id, season, team_name)
    if not resolved:
        raise OwnershipError("That is not one of the imported league teams.")
    team, external_team_id = resolved
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT 1 FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
            (guild_id, season, team),
        )
        if not await cursor.fetchone():
            await conn.rollback()
            raise OwnershipError("That team is no longer available.")
        cursor = await conn.execute(
            "SELECT team_name,approved FROM profiles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        current = await cursor.fetchone()
        if current:
            await conn.rollback()
            if current["approved"]:
                raise OwnershipError(f"You already own {current['team_name']}.")
            raise OwnershipError(
                f"You already have a pending claim for {current['team_name']}."
            )
        cursor = await conn.execute(
            """SELECT 1 FROM profiles WHERE guild_id=?
               AND (external_team_id=? OR lower(team_name)=lower(?)) LIMIT 1""",
            (guild_id, external_team_id, team),
        )
        if await cursor.fetchone():
            await conn.rollback()
            raise OwnershipError("Another member already owns or has a pending claim for that team.")
        await conn.execute(
            """INSERT INTO profiles
               (guild_id,user_id,team_name,external_team_id,approved,assignment_source,updated_at)
               VALUES (?,?,?,?,0,'open_team_button',?)""",
            (guild_id, user_id, team, external_team_id, now),
        )
        await conn.commit()
    return Assignment(
        user_id, team, source="open_team_button", external_team_id=external_team_id
    )


async def decide_team_claim(
    db: Database, guild_id: int, season: str, user_id: int, *,
    approved: bool, decided_by: int,
) -> Assignment:
    """Approve or deny a reserved claim atomically."""
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            """SELECT * FROM profiles WHERE guild_id=? AND user_id=? AND approved=0""",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            await conn.rollback()
            raise OwnershipError("This claim is no longer pending.")
        team = row["team_name"]
        external_team_id = row["external_team_id"]
        if approved:
            await conn.execute(
                """UPDATE profiles SET approved=1,approved_by=?,assignment_source='commissioner_approval',
                   assigned_at=?,updated_at=? WHERE id=? AND approved=0""",
                (decided_by, now, now, row["id"]),
            )
            await conn.execute(
                "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
                (guild_id, season, team),
            )
            await conn.execute(
                """UPDATE matchups SET
                   away_user_id=CASE WHEN away_team_id=? OR lower(away_team)=lower(?) THEN ? ELSE away_user_id END,
                   home_user_id=CASE WHEN home_team_id=? OR lower(home_team)=lower(?) THEN ? ELSE home_user_id END,
                   updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
                   ('complete','force_home','force_away','fair_sim')""",
                (
                    external_team_id, team, user_id, external_team_id, team, user_id,
                    now, guild_id, season,
                ),
            )
            await ensure_participant(
                conn, guild_id=guild_id, season=season, user_id=user_id, team_name=team
            )
        else:
            await conn.execute("DELETE FROM profiles WHERE id=? AND approved=0", (row["id"],))
            await conn.execute(
                """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                   VALUES (?,?,?,'Available for self-service claim',?)
                   ON CONFLICT(guild_id,season,team_name) DO UPDATE SET updated_at=excluded.updated_at""",
                (guild_id, season, team, now),
            )
        await conn.commit()
    return Assignment(
        user_id, team, source="commissioner_approval", external_team_id=external_team_id
    )


async def assign_team_directly(
    db: Database, guild_id: int, season: str, user_id: int, team_name: str, *,
    assigned_by: int, replace_existing: bool = False,
) -> Assignment:
    """Commissioner assignment, optionally replacing the team's current owner."""
    resolved = await resolve_team(db, guild_id, season, team_name)
    if not resolved:
        raise OwnershipError("That is not one of the imported league teams.")
    team, external_team_id = resolved
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT * FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        target_profile = await cursor.fetchone()
        if target_profile and normalize_team_name(target_profile["team_name"]) != normalize_team_name(team):
            await conn.rollback()
            raise OwnershipError(
                f"That member already has an active assignment for {target_profile['team_name']}."
            )
        cursor = await conn.execute(
            """SELECT * FROM profiles WHERE guild_id=?
               AND (external_team_id=? OR lower(team_name)=lower(?)) AND user_id<>? LIMIT 1""",
            (guild_id, external_team_id, team, user_id),
        )
        previous = await cursor.fetchone()
        if previous and not replace_existing:
            await conn.rollback()
            raise OwnershipError(
                "That team already has an owner or pending claim. Set replace_existing to True to reassign it."
            )
        if previous and previous["approved"]:
            cursor = await conn.execute(
                """SELECT 1 FROM matchups WHERE guild_id=? AND season=?
                   AND status IN ('result_pending','issue_reported')
                   AND (away_user_id=? OR home_user_id=?) LIMIT 1""",
                (guild_id, season, previous["user_id"], previous["user_id"]),
            )
            if await cursor.fetchone():
                await conn.rollback()
                raise OwnershipError(
                    "The current owner has a result under review. Resolve it before reassigning the team."
                )
        if previous:
            await conn.execute("DELETE FROM profiles WHERE id=?", (previous["id"],))
        await conn.execute(
            """INSERT INTO profiles
               (guild_id,user_id,team_name,external_team_id,approved,approved_by,
                assignment_source,assigned_at,updated_at)
               VALUES (?,?,?,?,1,?,'commissioner_direct',?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET
               team_name=excluded.team_name,external_team_id=excluded.external_team_id,
               approved=1,approved_by=excluded.approved_by,
               assignment_source=excluded.assignment_source,assigned_at=excluded.assigned_at,
               updated_at=excluded.updated_at""",
            (guild_id, user_id, team, external_team_id, assigned_by, now, now),
        )
        await conn.execute(
            "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
            (guild_id, season, team),
        )
        await conn.execute(
            """UPDATE matchups SET
               away_user_id=CASE WHEN away_team_id=? OR lower(away_team)=lower(?) THEN ? ELSE away_user_id END,
               home_user_id=CASE WHEN home_team_id=? OR lower(home_team)=lower(?) THEN ? ELSE home_user_id END,
               updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
               ('complete','force_home','force_away','fair_sim','result_pending','issue_reported')""",
            (
                external_team_id, team, user_id, external_team_id, team, user_id,
                now, guild_id, season,
            ),
        )
        await ensure_participant(
            conn, guild_id=guild_id, season=season, user_id=user_id, team_name=team
        )
        await conn.commit()
    return Assignment(
        user_id, team, source="commissioner_direct", external_team_id=external_team_id,
        previous_user_id=previous["user_id"] if previous else None,
    )


def canonical_team(value: str) -> str | None:
    """Legacy synchronous resolver retained for old Madden/member CSV tests."""
    lookup = {normalize_team_name(team): team for team in MADDEN_TEAMS}
    return lookup.get(normalize_team_name(value))


async def resolve_team(
    db: Database, guild_id: int, season: str, value: str
) -> tuple[str, str | None] | None:
    row = await db.fetchone(
        """SELECT external_team_id,team_name FROM franchises
           WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)""",
        (guild_id, season, value.strip()),
    )
    if row:
        return row["team_name"], row["external_team_id"]
    fallback = canonical_team(value)
    return (fallback, fallback.casefold()) if fallback else None


async def claim_team(
    db: Database, guild_id: int, season: str, user_id: int, team_name: str, *,
    source: str = "self_claim", require_open: bool = True,
) -> Assignment:
    resolved = await resolve_team(db, guild_id, season, team_name)
    if not resolved:
        raise OwnershipError("That is not one of the imported league teams.")
    team, external_team_id = resolved
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if require_open:
            cursor = await conn.execute(
                "SELECT 1 FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
                (guild_id, season, team),
            )
            if not await cursor.fetchone():
                await conn.rollback()
                raise OwnershipError("That team is not currently open for self-service claims.")
        cursor = await conn.execute(
            """SELECT user_id FROM profiles WHERE guild_id=?
               AND (external_team_id=? OR lower(team_name)=lower(?)) AND user_id<>?""",
            (guild_id, external_team_id, team, user_id),
        )
        if await cursor.fetchone():
            await conn.rollback()
            raise OwnershipError("Another member already owns or has a pending claim for that team.")
        cursor = await conn.execute(
            "SELECT team_name FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        current = await cursor.fetchone()
        if current and normalize_team_name(current["team_name"]) != normalize_team_name(team):
            await conn.rollback()
            raise OwnershipError(f"You already have an active assignment for {current['team_name']}.")
        await conn.execute(
            """INSERT INTO profiles
               (guild_id,user_id,team_name,external_team_id,approved,approved_by,
                assignment_source,assigned_at,updated_at)
               VALUES (?,?,?,?,1,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
               team_name=excluded.team_name,external_team_id=excluded.external_team_id,
               approved=1,approved_by=excluded.approved_by,
               assignment_source=excluded.assignment_source,assigned_at=excluded.assigned_at,
               updated_at=excluded.updated_at""",
            (guild_id, user_id, team, external_team_id, user_id, source, now, now),
        )
        await conn.execute(
            "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
            (guild_id, season, team),
        )
        await conn.execute(
            """UPDATE matchups SET
               away_user_id=CASE WHEN away_team_id=? OR lower(away_team)=lower(?) THEN ? ELSE away_user_id END,
               home_user_id=CASE WHEN home_team_id=? OR lower(home_team)=lower(?) THEN ? ELSE home_user_id END,
               updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
               ('complete','force_home','force_away','fair_sim')""",
            (
                external_team_id, team, user_id, external_team_id, team, user_id,
                now, guild_id, season,
            ),
        )
        await ensure_participant(
            conn, guild_id=guild_id, season=season, user_id=user_id, team_name=team
        )
        await conn.commit()
    return Assignment(user_id, team, source=source, external_team_id=external_team_id)


async def release_assignment(
    db: Database, guild_id: int, season: str, user_id: int, *, make_open: bool = True
) -> str | None:
    now = iso_now()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT team_name FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        row = await cursor.fetchone()
        if not row:
            await conn.rollback()
            return None
        team = row["team_name"]
        cursor = await conn.execute(
            """SELECT 1 FROM matchups WHERE guild_id=? AND season=?
               AND result_submitted_by IS NOT NULL AND status IN ('result_pending','issue_reported')
               AND (away_user_id=? OR home_user_id=?) LIMIT 1""",
            (guild_id, season, user_id, user_id),
        )
        if await cursor.fetchone():
            await conn.rollback()
            raise OwnershipError("This owner has a result under review. Resolve it before releasing the team.")
        await conn.execute("DELETE FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await conn.execute(
            """UPDATE matchups SET
               away_user_id=CASE WHEN away_user_id=? THEN NULL ELSE away_user_id END,
               home_user_id=CASE WHEN home_user_id=? THEN NULL ELSE home_user_id END,
               updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
               ('complete','force_home','force_away','fair_sim')""",
            (user_id, user_id, now, guild_id, season),
        )
        if make_open:
            await conn.execute(
                """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                   VALUES (?,?,?,'Available for self-service claim',?)
                   ON CONFLICT(guild_id,season,team_name) DO UPDATE SET updated_at=excluded.updated_at""",
                (guild_id, season, team, now),
            )
        await conn.commit()
    return team


async def initialize_open_teams(db: Database, guild_id: int, season: str) -> None:
    now = iso_now()
    teams = await active_team_names(db, guild_id, season)
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        for team in teams:
            cursor = await conn.execute(
                "SELECT 1 FROM profiles WHERE guild_id=? AND lower(team_name)=lower(?)",
                (guild_id, team),
            )
            if not await cursor.fetchone():
                await conn.execute(
                    """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                       VALUES (?,?,?,'Available for self-service claim',?)
                       ON CONFLICT(guild_id,season,team_name) DO NOTHING""",
                    (guild_id, season, team, now),
                )
        await conn.commit()


async def sync_assignment_discord(
    client: discord.Client, db: Database, guild: discord.Guild, assignment: Assignment
) -> list[str]:
    errors: list[str] = []
    member = guild.get_member(assignment.user_id)
    if member:
        try:
            await assign_team_role(db, member, assignment.team_name)
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            errors.append(str(exc))
    from .channel_workflow import refresh_matchup_message
    rows = await db.fetchall(
        """SELECT id FROM matchups WHERE guild_id=? AND channel_id IS NOT NULL
           AND status NOT IN ('complete','force_home','force_away','fair_sim')
           AND (away_user_id=? OR home_user_id=?)""",
        (guild.id, assignment.user_id, assignment.user_id),
    )
    for row in rows:
        try:
            await refresh_matchup_message(client, db, row["id"])
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"matchup {row['id']}: {exc}")
    return errors


async def sync_all_member_roles(guild: discord.Guild, db: Database) -> list[str]:
    errors: list[str] = []
    rows = await db.fetchall("SELECT * FROM profiles WHERE guild_id=? AND approved=1", (guild.id,))
    for row in rows:
        member = guild.get_member(row["user_id"])
        if not member:
            errors.append(f"{row['user_id']}: member is no longer in this server")
            continue
        try:
            await assign_team_role(db, member, row["team_name"])
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"{row['user_id']}/{row['team_name']}: {exc}")
    return errors

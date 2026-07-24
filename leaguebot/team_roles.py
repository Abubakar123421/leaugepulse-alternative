from __future__ import annotations

import discord

from .db import Database
from .helpers import iso_now


MADDEN_TEAMS = (
    "49ers", "Bears", "Bengals", "Bills", "Broncos", "Browns", "Buccaneers",
    "Cardinals", "Chargers", "Chiefs", "Colts", "Commanders", "Cowboys",
    "Dolphins", "Eagles", "Falcons", "Giants", "Jaguars", "Jets", "Lions",
    "Packers", "Panthers", "Patriots", "Raiders", "Rams", "Ravens", "Saints",
    "Seahawks", "Steelers", "Texans", "Titans", "Vikings",
)


async def active_team_names(db: Database, guild_id: int, season: str) -> tuple[str, ...]:
    rows = await db.fetchall(
        """SELECT team_name FROM franchises WHERE guild_id=? AND season=?
           ORDER BY sort_order,team_name""",
        (guild_id, season),
    )
    return tuple(row["team_name"] for row in rows) or MADDEN_TEAMS


async def active_franchises(db: Database, guild_id: int, season: str):
    rows = await db.fetchall(
        """SELECT * FROM franchises WHERE guild_id=? AND season=?
           ORDER BY sort_order,team_name""",
        (guild_id, season),
    )
    if rows:
        return rows
    return [
        {"external_team_id": team.casefold(), "team_name": team, "abbreviation": team[:3].upper()}
        for team in MADDEN_TEAMS
    ]


async def ensure_team_roles(
    guild: discord.Guild, db: Database, season: str
) -> tuple[int, list[str]]:
    created = 0
    errors: list[str] = []
    existing = {role.name.casefold(): role for role in guild.roles}
    for team in await active_team_names(db, guild.id, season):
        mapped = await db.fetchone(
            "SELECT role_id FROM team_roles WHERE guild_id=? AND lower(team_name)=lower(?)",
            (guild.id, team),
        )
        role = guild.get_role(mapped["role_id"]) if mapped else None
        if role is None:
            role = existing.get(team.casefold())
        if role is None:
            try:
                role = await guild.create_role(
                    name=team,
                    permissions=discord.Permissions.none(),
                    mentionable=False,
                    reason="League team role provisioning",
                )
                created += 1
                existing[team.casefold()] = role
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"{team}: {exc}")
                continue
        await db.execute(
            """INSERT INTO team_roles (guild_id,team_name,role_id,updated_at)
               VALUES (?,?,?,?) ON CONFLICT(guild_id,team_name) DO UPDATE SET
               role_id=excluded.role_id,updated_at=excluded.updated_at""",
            (guild.id, team, role.id, iso_now()),
        )
    return created, errors


async def ensure_madden_team_roles(
    guild: discord.Guild, db: Database
) -> tuple[int, list[str]]:
    settings = await db.settings(guild.id)
    return await ensure_team_roles(guild, db, settings["season"])


async def team_role(db: Database, guild: discord.Guild, team_name: str):
    row = await db.fetchone(
        """SELECT role_id FROM team_roles
           WHERE guild_id=? AND lower(team_name)=lower(?)""",
        (guild.id, team_name),
    )
    return guild.get_role(row["role_id"]) if row else None


async def assign_team_role(
    db: Database, member: discord.Member, team_name: str
) -> None:
    role = await team_role(db, member.guild, team_name)
    if role is None:
        raise ValueError(f"The {team_name} role is missing. Run /setup or /importrosters to repair roles.")
    managed_ids = {
        row["role_id"]
        for row in await db.fetchall(
            "SELECT role_id FROM team_roles WHERE guild_id=?", (member.guild.id,)
        )
    }
    remove = [item for item in member.roles if item.id in managed_ids and item.id != role.id]
    if remove:
        await member.remove_roles(*remove, reason="League team ownership changed")
    if role not in member.roles:
        await member.add_roles(role, reason=f"Approved owner of {team_name}")


async def remove_team_role(
    db: Database, member: discord.Member, team_name: str
) -> None:
    role = await team_role(db, member.guild, team_name)
    if role and role in member.roles:
        await member.remove_roles(role, reason="League team ownership released")


async def clear_team_role_members(guild: discord.Guild, db: Database) -> list[str]:
    errors: list[str] = []
    for row in await db.fetchall(
        "SELECT team_name,role_id FROM team_roles WHERE guild_id=?", (guild.id,)
    ):
        role = guild.get_role(row["role_id"])
        if not role:
            continue
        for member in list(role.members):
            try:
                await member.remove_roles(role, reason="League season ownership cleared")
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"{row['team_name']}/{member.id}: {exc}")
    return errors

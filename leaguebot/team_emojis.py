"""Resolve per-guild team emojis by name without persisting Discord emoji IDs."""

from __future__ import annotations

import discord

from .db import Database

NFL_EMOJI_BY_ABBR: dict[str, tuple[str, ...]] = {
    "ARI": ("ArizonaCardinals",), "AZ": ("ArizonaCardinals",), "ATL": ("AtlantaFalcons",),
    "BAL": ("BaltimoreRavens",), "BUF": ("BuffaloBills",),
    "CAR": ("CarolinaPanthers",), "CHI": ("ChicagoBears",),
    "CIN": ("CincinnatiBengals", "CinncinatiBengals"),
    "CLE": ("ClevelandBrowns",), "DAL": ("DallasCowboys",),
    "DEN": ("DenverBroncos",), "DET": ("DetroitLions",),
    "GB": ("GreenBayPackers",), "HOU": ("HoustonTexans",),
    "IND": ("IndianapolisColts",), "JAX": ("JacksonvilleJaguars",),
    "KC": ("KansasCityChiefs",), "LV": ("LasVegasRaiders",),
    "LAC": ("LosAngelesChargers",), "LAR": ("LosAngelesRams",),
    "MIA": ("MiamiDolphins",), "MIN": ("MinnesotaVikings",),
    "NE": ("NewEnglandPatriots",), "NO": ("NewOrleansSaints",),
    "NYG": ("NewYorkGiants",), "NYJ": ("NewYorkJets",),
    "PHI": ("PhiladelphiaEagles",), "PIT": ("PittsburghSteelers",),
    "SEA": ("SeattleSeahawks",),
    "SF": ("SanFrancisco49ers", "SanFrancisco49ners"),
    "TB": ("TampaBayBuccaneers",), "TEN": ("TennesseeTitans",),
    "WAS": ("WashingtonCommanders",),
}


def emoji_named(guild: discord.Guild, name: str | None) -> discord.Emoji | None:
    if not name:
        return None
    wanted = name.strip().strip(":").casefold()
    return next((emoji for emoji in guild.emojis if emoji.name.casefold() == wanted), None)


async def franchise_row(db: Database, guild_id: int, season: str, team_name: str):
    return await db.fetchone(
        """SELECT * FROM franchises WHERE guild_id=? AND season=?
           AND lower(team_name)=lower(?)""",
        (guild_id, season, team_name),
    )


async def team_emoji(db: Database, guild: discord.Guild, season: str, team_name: str) -> discord.Emoji | None:
    franchise = await franchise_row(db, guild.id, season, team_name)
    if not franchise:
        return None
    configured = emoji_named(guild, franchise["emoji_name"])
    if configured:
        return configured
    for candidate in NFL_EMOJI_BY_ABBR.get((franchise["abbreviation"] or "").upper(), ()):
        resolved = emoji_named(guild, candidate)
        if resolved:
            return resolved
    return None


async def team_label(db: Database, guild: discord.Guild, season: str, team_name: str, *, bold: bool = False) -> str:
    emoji = await team_emoji(db, guild, season, team_name)
    name = f"**{team_name}**" if bold else team_name
    return f"{emoji} {name}" if emoji else name


async def team_vote_reaction(db: Database, guild: discord.Guild, season: str, team_name: str, fallback: str) -> discord.Emoji | str:
    return await team_emoji(db, guild, season, team_name) or fallback


async def sync_team_emojis(db: Database, guild: discord.Guild, season: str) -> tuple[int, list[str]]:
    franchises = await db.fetchall(
        """SELECT external_team_id,team_name,abbreviation,emoji_name
           FROM franchises WHERE guild_id=? AND season=?""", (guild.id, season),
    )
    matched = 0
    missing: list[str] = []
    for franchise in franchises:
        resolved = emoji_named(guild, franchise["emoji_name"])
        if resolved is None:
            for candidate in NFL_EMOJI_BY_ABBR.get((franchise["abbreviation"] or "").upper(), ()):
                resolved = emoji_named(guild, candidate)
                if resolved:
                    break
        if resolved:
            await db.execute(
                """UPDATE franchises SET emoji_name=?
                   WHERE guild_id=? AND season=? AND external_team_id=?""",
                (resolved.name, guild.id, season, franchise["external_team_id"]),
            )
            matched += 1
        else:
            missing.append(franchise["team_name"])
    return matched, sorted(missing, key=str.casefold)
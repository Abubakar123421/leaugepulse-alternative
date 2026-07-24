from __future__ import annotations

import json
from typing import Any

import discord

from .ai import sanitize_ai_text
from .db import Database
from .helpers import FINAL_STATUSES, iso_now
from .team_emojis import team_label


NFL_ALIGNMENT = {
    "BUF": ("AFC", "East"), "MIA": ("AFC", "East"), "NE": ("AFC", "East"), "NYJ": ("AFC", "East"),
    "BAL": ("AFC", "North"), "CIN": ("AFC", "North"), "CLE": ("AFC", "North"), "PIT": ("AFC", "North"),
    "HOU": ("AFC", "South"), "IND": ("AFC", "South"), "JAX": ("AFC", "South"), "TEN": ("AFC", "South"),
    "DEN": ("AFC", "West"), "KC": ("AFC", "West"), "LV": ("AFC", "West"), "LAC": ("AFC", "West"),
    "DAL": ("NFC", "East"), "NYG": ("NFC", "East"), "PHI": ("NFC", "East"), "WAS": ("NFC", "East"),
    "CHI": ("NFC", "North"), "DET": ("NFC", "North"), "GB": ("NFC", "North"), "MIN": ("NFC", "North"),
    "ATL": ("NFC", "South"), "CAR": ("NFC", "South"), "NO": ("NFC", "South"), "TB": ("NFC", "South"),
    "ARI": ("NFC", "West"), "LAR": ("NFC", "West"), "SF": ("NFC", "West"), "SEA": ("NFC", "West"),
}


def _blank_record() -> dict[str, int]:
    return {"wins": 0, "losses": 0, "ties": 0, "pf": 0, "pa": 0, "recent": 0}


def _rank_key(row: dict[str, Any]) -> tuple:
    games = row["wins"] + row["losses"] + row["ties"]
    pct = (row["wins"] + 0.5 * row["ties"]) / games if games else 0.0
    return (-pct, -(row["pf"] - row["pa"]), -row["recent"], row["team"].casefold())


def _apply_game(records: dict[str, dict[str, int]], game, *, recent: bool = False) -> None:
    away = records.setdefault(game["away_team"], _blank_record())
    home = records.setdefault(game["home_team"], _blank_record())
    status = game["status"]
    if status == "complete" and game["away_score"] is not None and game["home_score"] is not None:
        away["pf"] += game["away_score"]
        away["pa"] += game["home_score"]
        home["pf"] += game["home_score"]
        home["pa"] += game["away_score"]
        if game["away_score"] > game["home_score"]:
            away["wins"] += 1
            home["losses"] += 1
            if recent:
                away["recent"] += 1
        elif game["home_score"] > game["away_score"]:
            home["wins"] += 1
            away["losses"] += 1
            if recent:
                home["recent"] += 1
        else:
            away["ties"] += 1
            home["ties"] += 1
    elif status == "force_away":
        away["wins"] += 1
        home["losses"] += 1
        if recent:
            away["recent"] += 1
    elif status == "force_home":
        home["wins"] += 1
        away["losses"] += 1
        if recent:
            home["recent"] += 1


async def weekly_facts(db: Database, guild_id: int, season: str, week: int) -> dict[str, Any]:
    franchises = await db.fetchall(
        """SELECT team_name,abbreviation FROM franchises
           WHERE guild_id=? AND season=?""",
        (guild_id, season),
    )
    all_games = await db.fetchall(
        """SELECT * FROM matchups WHERE guild_id=? AND season=? AND week<=?
           ORDER BY week,id""",
        (guild_id, season, week),
    )
    records = {row["team_name"]: _blank_record() for row in franchises}
    preweek = {row["team_name"]: _blank_record() for row in franchises}
    for game in all_games:
        if game["week"] < week and game["status"] in FINAL_STATUSES:
            _apply_game(preweek, game)
        if game["status"] in FINAL_STATUSES:
            _apply_game(records, game, recent=game["week"] >= max(1, week - 2))

    scored = [
        game for game in all_games
        if game["week"] == week and game["status"] == "complete"
        and game["away_score"] is not None and game["home_score"] is not None
    ]
    unresolved = sum(
        game["week"] == week and game["status"] not in FINAL_STATUSES
        for game in all_games
    )
    best = min(
        scored,
        key=lambda g: (abs(g["away_score"] - g["home_score"]), -(g["away_score"] + g["home_score"]), g["id"]),
        default=None,
    )
    blowout = max(
        scored,
        key=lambda g: (abs(g["away_score"] - g["home_score"]), max(g["away_score"], g["home_score"]), -g["id"]),
        default=None,
    )
    upset_candidates = []
    for game in scored:
        winner = game["away_team"] if game["away_score"] > game["home_score"] else game["home_team"]
        loser = game["home_team"] if winner == game["away_team"] else game["away_team"]
        wr, lr = preweek[winner], preweek[loser]
        wg = wr["wins"] + wr["losses"] + wr["ties"]
        lg = lr["wins"] + lr["losses"] + lr["ties"]
        wp = (wr["wins"] + 0.5 * wr["ties"]) / wg if wg else 0.0
        lp = (lr["wins"] + 0.5 * lr["ties"]) / lg if lg else 0.0
        gap = lp - wp
        pd_gap = (lr["pf"] - lr["pa"]) - (wr["pf"] - wr["pa"])
        if gap > 0 or (gap == 0 and pd_gap > 0):
            upset_candidates.append((gap, pd_gap, abs(game["away_score"] - game["home_score"]), game))
    upset = max(upset_candidates, default=None, key=lambda item: item[:3])

    ranked = []
    abbreviations = {row["team_name"]: (row["abbreviation"] or "").upper() for row in franchises}
    for team, record in records.items():
        ranked.append({"team": team, "abbreviation": abbreviations.get(team, ""), **record})
    ranked.sort(key=_rank_key)
    for index, row in enumerate(ranked, 1):
        row["rank"] = index

    playoffs: dict[str, list[dict[str, Any]]] = {"AFC": [], "NFC": []}
    for conference in playoffs:
        members = [row for row in ranked if NFL_ALIGNMENT.get(row["abbreviation"], (None, None))[0] == conference]
        leaders = []
        for division in ("East", "North", "South", "West"):
            division_rows = [row for row in members if NFL_ALIGNMENT.get(row["abbreviation"]) == (conference, division)]
            if division_rows:
                leaders.append(sorted(division_rows, key=_rank_key)[0])
        leaders.sort(key=_rank_key)
        remaining = [row for row in members if row not in leaders]
        playoffs[conference] = leaders + sorted(remaining, key=_rank_key)[:3]

    mvp = await db.fetchone(
        """SELECT * FROM weekly_mvps WHERE guild_id=? AND season=? AND week=?""",
        (guild_id, season, week),
    )

    def game_fact(game):
        if not game:
            return None
        return {
            "away_team": game["away_team"], "home_team": game["home_team"],
            "away_score": game["away_score"], "home_score": game["home_score"],
        }

    return {
        "week": week,
        "unresolved": unresolved,
        "upset": game_fact(upset[3] if upset else None),
        "upset_strength": (
            {"record_gap": upset[0], "point_diff_gap": upset[1]}
            if upset else None
        ),
        "best_game": game_fact(best),
        "biggest_blowout": game_fact(blowout),
        "mvp": dict(mvp) if mvp else None,
        "top_five": ranked[:5],
        "playoffs": playoffs,
    }


async def _game_text(db: Database, guild: discord.Guild, season: str, game: dict | None) -> str:
    if not game:
        return "No qualifying completed game."
    away = await team_label(db, guild, season, game["away_team"])
    home = await team_label(db, guild, season, game["home_team"])
    return f"{away} **{game['away_score']}–{game['home_score']}** {home}"


async def weekly_recap_embed(
    db: Database,
    guild: discord.Guild,
    season: str,
    facts: dict[str, Any],
    narrative: str | None = None,
) -> discord.Embed:
    week = facts["week"]
    embed = discord.Embed(
        title=f"🏈 Week {week} Weekly Recap",
        description=(narrative or "Official deterministic league recap.")[:4096],
        color=discord.Color.purple(),
    )
    embed.add_field(name="Upset of the Week", value=await _game_text(db, guild, season, facts["upset"]), inline=False)
    embed.add_field(name="Best Game", value=await _game_text(db, guild, season, facts["best_game"]), inline=False)
    embed.add_field(name="Biggest Blowout", value=await _game_text(db, guild, season, facts["biggest_blowout"]), inline=False)
    mvp = facts["mvp"]
    if mvp:
        mvp_team = await team_label(db, guild, season, mvp["team_name"])
        mvp_text = f"**{mvp['player_name']}** · {mvp_team}\n{mvp['stats_text']}"
    else:
        mvp_text = "**Awaiting Commissioner Selection**\nUse `/weekmvp` to update this recap."
    embed.add_field(name="MVP Performance", value=mvp_text[:1024], inline=False)
    top_lines = []
    for row in facts["top_five"]:
        label = await team_label(db, guild, season, row["team"])
        top_lines.append(
            f"**{row['rank']}.** {label} · {row['wins']}-{row['losses']}-{row['ties']} · PD {row['pf'] - row['pa']:+d}"
        )
    embed.add_field(name="Top 5 Teams", value="\n".join(top_lines) or "No results yet.", inline=False)
    for conference in ("AFC", "NFC"):
        lines = []
        for seed, row in enumerate(facts["playoffs"][conference], 1):
            label = await team_label(db, guild, season, row["team"])
            lines.append(f"**{seed}.** {label} · {row['wins']}-{row['losses']}-{row['ties']}")
        embed.add_field(name=f"Projected {conference} Playoff Picture", value="\n".join(lines) or "No projection yet.", inline=False)
    if facts["unresolved"]:
        embed.add_field(
            name="Pending Results",
            value=f"{facts['unresolved']} matchup(s) remain unresolved. This post updates automatically.",
            inline=False,
        )
    embed.set_footer(text="Projection ties: win percentage, point differential, recent form, team name")
    return embed


async def publish_weekly_recap(
    client: discord.Client,
    db: Database,
    guild_id: int,
    season: str,
    week: int,
    *,
    create: bool = True,
    regenerate_ai: bool = True,
) -> tuple[bool, str]:
    guild = client.get_guild(guild_id)
    if not guild:
        return False, "Guild is unavailable."
    settings = await db.settings(guild_id)
    channel = guild.get_channel(settings.get("storyline_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel):
        return False, "Configure `/setstorylinechannel` first."
    existing = await db.fetchone(
        "SELECT * FROM weekly_recaps WHERE guild_id=? AND season=? AND week=?",
        (guild_id, season, week),
    )
    if not existing and not create:
        return False, "No recap exists yet."
    facts = await weekly_facts(db, guild_id, season, week)
    narrative = existing["narrative_text"] if existing else None
    ai = getattr(client, "ai", None)
    if regenerate_ai and ai and ai.available and settings.get("ai_enabled", 1):
        prompt = (
            "Write one compact Madden weekly recap introduction under 180 words using only "
            "the following deterministic JSON. Do not invent plays, player statistics, "
            f"injuries, quotes, or rankings. JSON: {json.dumps(facts, default=str)}"
        )
        try:
            narrative = sanitize_ai_text(await ai.generate(guild_id, prompt), 1800)
        except Exception:
            narrative = narrative
    embed = await weekly_recap_embed(db, guild, season, facts, narrative)
    message = None
    if existing and existing["message_id"]:
        try:
            message = channel.get_partial_message(existing["message_id"])
            await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    if message is None:
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    now = iso_now()
    await db.execute(
        """INSERT INTO weekly_recaps
           (guild_id,season,week,channel_id,message_id,facts_json,narrative_text,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guild_id,season,week) DO UPDATE SET
           channel_id=excluded.channel_id,message_id=excluded.message_id,
           facts_json=excluded.facts_json,narrative_text=excluded.narrative_text,
           status=excluded.status,updated_at=excluded.updated_at""",
        (
            guild_id, season, week, channel.id, message.id,
            json.dumps(facts, default=str), narrative,
            "complete" if not facts["unresolved"] and facts["mvp"] else "partial",
            now, now,
        ),
    )
    return True, "Weekly recap posted." if not existing else "Weekly recap updated."


async def upsert_week_mvp(
    client: discord.Client,
    db: Database,
    guild_id: int,
    season: str,
    week: int,
    player: str,
    team: str,
    stats: str,
    entered_by: int,
) -> None:
    now = iso_now()
    await db.execute(
        """INSERT INTO weekly_mvps
           (guild_id,season,week,player_name,team_name,stats_text,entered_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guild_id,season,week) DO UPDATE SET
           player_name=excluded.player_name,team_name=excluded.team_name,
           stats_text=excluded.stats_text,entered_by=excluded.entered_by,
           updated_at=excluded.updated_at""",
        (guild_id, season, week, player, team, stats, entered_by, now, now),
    )
    await publish_weekly_recap(
        client, db, guild_id, season, week, create=False, regenerate_ai=False
    )

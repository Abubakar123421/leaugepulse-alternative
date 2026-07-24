from __future__ import annotations

import discord

from .db import Database
from .helpers import iso_now
from .team_emojis import team_label, team_vote_reaction


ALLOWED_GRAPHICS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_GRAPHIC_BYTES = 8 * 1024 * 1024


async def post_game_of_week(
    client: discord.Client,
    db: Database,
    *,
    guild: discord.Guild,
    season: str,
    week: int,
    matchup,
    graphic: discord.Attachment,
    actor_id: int,
) -> tuple[bool, str]:
    if graphic.content_type not in ALLOWED_GRAPHICS:
        return False, "Attach a PNG, JPG, or WebP graphic."
    if graphic.size > MAX_GRAPHIC_BYTES:
        return False, "The graphic must be 8 MB or smaller."
    existing = await db.fetchone(
        """SELECT channel_id,message_id FROM game_of_week_posts
           WHERE guild_id=? AND season=? AND week=?""",
        (guild.id, season, week),
    )
    if existing:
        return False, f"Week {week} already has a Game of the Week post."
    settings = await db.settings(guild.id)
    channel = guild.get_channel(settings.get("polls_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel):
        return False, "Configure `/setpollchannel` first."
    away = await team_label(db, guild, season, matchup["away_team"], bold=True)
    home = await team_label(db, guild, season, matchup["home_team"], bold=True)
    extension = ALLOWED_GRAPHICS[graphic.content_type]
    filename = f"season-{season}-week-{week}-game-of-the-week.{extension}"
    upload = await graphic.to_file(filename=filename)
    embed = discord.Embed(
        title=f"🏆 Week {week} Game of the Week",
        description=(
            f"{away} at {home}\n\n"
            "Who wins?\n"
            "React with your team’s emoji below to vote."
        ),
        color=discord.Color.gold(),
    )
    embed.set_image(url=f"attachment://{filename}")
    message = await channel.send(
        embed=embed,
        file=upload,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    away_reaction = await team_vote_reaction(
        db, guild, season, matchup["away_team"], "1️⃣"
    )
    home_reaction = await team_vote_reaction(
        db, guild, season, matchup["home_team"], "2️⃣"
    )
    await message.add_reaction(away_reaction)
    await message.add_reaction(home_reaction)
    await db.execute(
        """INSERT INTO game_of_week_posts
           (guild_id,season,week,matchup_id,channel_id,message_id,created_by,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            guild.id, season, week, matchup["id"], channel.id,
            message.id, actor_id, iso_now(),
        ),
    )
    return True, f"Game of the Week posted in {channel.mention}."

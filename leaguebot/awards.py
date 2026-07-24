from __future__ import annotations

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import iso_now
from .team_emojis import team_label
from .weekly_content import weekly_facts


AWARD_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("mvp", "MVP"),
    ("offensive_player", "Offensive Player of the Year"),
    ("defensive_player", "Defensive Player of the Year"),
    ("rookie", "Rookie of the Year"),
    ("coach", "Coach of the Year"),
    ("user", "User of the Year"),
    ("best_stream", "Best Stream"),
    ("biggest_upset", "Biggest Upset"),
)
AWARD_LABELS = dict(AWARD_CATEGORIES)


async def set_season_award(
    db: Database,
    guild_id: int,
    season: str,
    category: str,
    recipient: str,
    details: str,
    actor_id: int,
    *,
    source: str = "commissioner",
) -> None:
    if category not in AWARD_LABELS:
        raise ValueError("Unknown season award category.")
    now = iso_now()
    await db.execute(
        """INSERT INTO season_awards
           (guild_id,season,category,recipient,details,source,entered_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guild_id,season,category) DO UPDATE SET
           recipient=excluded.recipient,details=excluded.details,source=excluded.source,
           entered_by=excluded.entered_by,updated_at=excluded.updated_at""",
        (
            guild_id, season, category, recipient.strip()[:300],
            details.strip()[:1000], source, actor_id, now, now,
        ),
    )
    await db.execute(
        """INSERT INTO season_award_summaries
           (guild_id,season,status,created_at,updated_at)
           VALUES (?,?,'draft',?,?)
           ON CONFLICT(guild_id,season) DO UPDATE SET
           status='draft',approved_by=NULL,approved_at=NULL,updated_at=excluded.updated_at""",
        (guild_id, season, now, now),
    )


async def ensure_award_suggestions(
    db: Database, guild_id: int, season: str, actor_id: int
) -> None:
    existing = {
        row["category"]
        for row in await db.fetchall(
            "SELECT category FROM season_awards WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
    }
    if "coach" not in existing:
        row = await db.fetchone(
            """SELECT t.name,t.wins,t.losses,t.ties,p.user_id
               FROM teams t LEFT JOIN profiles p
               ON p.guild_id=t.guild_id AND lower(p.team_name)=lower(t.name)
               WHERE t.guild_id=? AND t.season=?
               ORDER BY
               CASE WHEN t.wins+t.losses+t.ties=0 THEN 0.0
                    ELSE (t.wins + 0.5*t.ties)/(t.wins+t.losses+t.ties) END DESC,
               t.wins DESC,t.name LIMIT 1""",
            (guild_id, season),
        )
        if row and row["user_id"]:
            await set_season_award(
                db, guild_id, season, "coach",
                f"<@{row['user_id']}> · {row['name']}",
                f"Bot suggestion: league-best {row['wins']}-{row['losses']}-{row['ties']} record.",
                actor_id, source="suggested",
            )
    if "user" not in existing:
        row = await db.fetchone(
            """SELECT user_id,team_name,xp,games,wins FROM season_participants
               WHERE guild_id=? AND season=?
               ORDER BY xp DESC,games DESC,wins DESC,user_id LIMIT 1""",
            (guild_id, season),
        )
        if row:
            await set_season_award(
                db, guild_id, season, "user",
                f"<@{row['user_id']}> · {row['team_name']}",
                f"Bot suggestion: {row['xp']} XP across {row['games']} games.",
                actor_id, source="suggested",
            )
    if "biggest_upset" not in existing:
        max_week = await db.fetchone(
            "SELECT MAX(week) AS week FROM matchups WHERE guild_id=? AND season=?",
            (guild_id, season),
        )
        selected = None
        selected_key = None
        for week in range(1, (max_week["week"] if max_week and max_week["week"] else 0) + 1):
            facts = await weekly_facts(db, guild_id, season, week)
            strength = facts.get("upset_strength")
            key = (strength["record_gap"], strength["point_diff_gap"]) if strength else None
            if facts["upset"] and (selected_key is None or key > selected_key):
                selected = facts["upset"]
                selected_key = key
        if selected:
            await set_season_award(
                db, guild_id, season, "biggest_upset",
                f"{selected['away_team']} vs {selected['home_team']}",
                f"Bot suggestion: {selected['away_team']} {selected['away_score']}–"
                f"{selected['home_score']} {selected['home_team']}.",
                actor_id, source="suggested",
            )


async def season_awards_embed(
    db: Database, guild: discord.Guild, season: str
) -> discord.Embed:
    rows = {
        row["category"]: row
        for row in await db.fetchall(
            "SELECT * FROM season_awards WHERE guild_id=? AND season=?",
            (guild.id, season),
        )
    }
    summary = await db.fetchone(
        "SELECT * FROM season_award_summaries WHERE guild_id=? AND season=?",
        (guild.id, season),
    )
    embed = discord.Embed(
        title=f"🏆 Season {season} League Awards",
        description=(
            "Review every category before publishing. Bot suggestions are drafts and "
            "can be overridden with `/seasonaward`."
        ),
        color=discord.Color.gold(),
    )
    for key, label in AWARD_CATEGORIES:
        row = rows.get(key)
        value = (
            f"**{row['recipient']}**\n{row['details']}\n_Source: {row['source']}_"
            if row else "**Not selected**"
        )
        embed.add_field(name=label, value=value[:1024], inline=False)
    completed = len(rows)
    status = summary["status"] if summary else "draft"
    embed.set_footer(text=f"{completed}/{len(AWARD_CATEGORIES)} complete · Status: {status}")
    return embed


async def awards_ready(db: Database, guild_id: int, season: str) -> bool:
    summary = await db.fetchone(
        """SELECT status FROM season_award_summaries
           WHERE guild_id=? AND season=?""",
        (guild_id, season),
    )
    count = await db.fetchone(
        "SELECT COUNT(*) AS total FROM season_awards WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    return bool(
        summary and summary["status"] == "published"
        and count and count["total"] == len(AWARD_CATEGORIES)
    )


async def publish_awards(
    client: discord.Client, db: Database, guild_id: int, season: str, actor_id: int
) -> tuple[bool, str]:
    guild = client.get_guild(guild_id)
    if not guild:
        return False, "Guild is unavailable."
    count = await db.fetchone(
        "SELECT COUNT(*) AS total FROM season_awards WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    if not count or count["total"] != len(AWARD_CATEGORIES):
        return False, f"Complete all {len(AWARD_CATEGORIES)} award categories first."
    settings = await db.settings(guild_id)
    channel = guild.get_channel(settings.get("storyline_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel):
        return False, "Configure `/setstorylinechannel` first."
    embed = await season_awards_embed(db, guild, season)
    summary = await db.fetchone(
        "SELECT * FROM season_award_summaries WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    message = None
    if summary and summary["message_id"]:
        try:
            message = channel.get_partial_message(summary["message_id"])
            await message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    if message is None:
        message = await channel.send(embed=embed)
    now = iso_now()
    await db.execute(
        """INSERT INTO season_award_summaries
           (guild_id,season,channel_id,message_id,approved_by,approved_at,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'published',?,?)
           ON CONFLICT(guild_id,season) DO UPDATE SET
           channel_id=excluded.channel_id,message_id=excluded.message_id,
           approved_by=excluded.approved_by,approved_at=excluded.approved_at,
           status='published',updated_at=excluded.updated_at""",
        (guild_id, season, channel.id, message.id, actor_id, now, now, now),
    )
    return True, "Season awards approved and published."


class SeasonAwardsApprovalView(discord.ui.View):
    def __init__(self, db: Database, guild_id: int, season: str, actor_id: int):
        super().__init__(timeout=180)
        self.db = db
        self.guild_id = guild_id
        self.season = season
        self.actor_id = actor_id

    @discord.ui.button(label="Approve & Publish Awards", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This awards review belongs to another Commissioner.", ephemeral=True
            )
            return
        settings = await self.db.settings(self.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only a Commissioner can publish awards.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ok, detail = await publish_awards(
            interaction.client, self.db, self.guild_id, self.season, interaction.user.id
        )
        button.disabled = ok
        await interaction.edit_original_response(content=detail, view=self)

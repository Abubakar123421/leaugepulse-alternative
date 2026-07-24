"""Apply the runtime team-emoji post refresh integration."""

from pathlib import Path


path = Path("leaguebot/bot.py")
text = path.read_text(encoding="utf-8")

class_anchor = "\nclass LeagueBot(discord.Client):\n"
helper = """

async def refresh_team_emoji_posts(
    bot: discord.Client, db: Database, guild: discord.Guild, season: str
) -> None:
    await refresh_open_teams_panel(bot, db, guild.id)
    rows = await db.fetchall(
        \"\"\"SELECT id FROM matchups
           WHERE guild_id=? AND season=? AND channel_id IS NOT NULL\"\"\",
        (guild.id, season),
    )
    for row in rows:
        await refresh_matchup_message(bot, db, row["id"])
"""
if "async def refresh_team_emoji_posts(" not in text:
    if class_anchor not in text:
        raise RuntimeError("LeagueBot class anchor not found")
    text = text.replace(class_anchor, helper + class_anchor, 1)

ready_anchor = """            if repaired:
"""
ready_replacement = """            await refresh_team_emoji_posts(
                self, self.db, guild, settings["season"]
            )
            if repaired:
"""
if ready_replacement not in text:
    if ready_anchor not in text:
        raise RuntimeError("on_ready refresh anchor not found")
    text = text.replace(ready_anchor, ready_replacement, 1)

sync_anchor = """        matched, missing = await sync_team_emojis(db, interaction.guild, settings["season"])
        detail = f"Matched **{matched}** franchise emoji(s) by name."
"""
sync_replacement = """        matched, missing = await sync_team_emojis(db, interaction.guild, settings["season"])
        await refresh_team_emoji_posts(bot, db, interaction.guild, settings["season"])
        detail = f"Matched **{matched}** franchise emoji(s) by name and refreshed team posts."
"""
if sync_replacement not in text:
    if sync_anchor not in text:
        raise RuntimeError("sync command anchor not found")
    text = text.replace(sync_anchor, sync_replacement, 1)

set_anchor = """            target_type="team", target_id=team, details={"emoji_name": resolved.name},
        )
        await interaction.response.send_message(
"""
set_replacement = """            target_type="team", target_id=team, details={"emoji_name": resolved.name},
        )
        await refresh_team_emoji_posts(bot, db, interaction.guild, settings["season"])
        await interaction.response.send_message(
"""
if set_replacement not in text:
    if set_anchor not in text:
        raise RuntimeError("set command anchor not found")
    text = text.replace(set_anchor, set_replacement, 1)

path.write_text(text, encoding="utf-8")

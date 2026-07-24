"""Read-only verification of custom emojis rendered in live Discord posts."""

from __future__ import annotations

import re
import sqlite3

from leaguebot.config import Config
from scripts.verify_live_rollout import get_json


config = Config.from_env()
connection = sqlite3.connect(config.database_path)
connection.row_factory = sqlite3.Row
guild_id = connection.execute(
    "SELECT guild_id FROM guild_settings ORDER BY guild_id LIMIT 1"
).fetchone()["guild_id"]

cards = connection.execute(
    """SELECT team_name,channel_id,message_id FROM open_team_cards
       WHERE guild_id=? ORDER BY team_name""",
    (guild_id,),
).fetchall()
rendered: list[str] = []
missing: list[str] = []
for card in cards:
    message = get_json(
        config.token, f"/channels/{card['channel_id']}/messages/{card['message_id']}"
    )
    embed = (message.get("embeds") or [{}])[0]
    text = " ".join(
        [
            embed.get("title") or "",
            embed.get("description") or "",
            *(field.get("value") or "" for field in embed.get("fields", [])),
        ]
    )
    if re.search(r"<a?:[A-Za-z0-9_]+:\d+>", text):
        rendered.append(card["team_name"])
    else:
        missing.append(card["team_name"])

print("cards_with_custom_emoji", len(rendered))
print("cards_without_custom_emoji", len(missing))
print("missing_cards", missing)

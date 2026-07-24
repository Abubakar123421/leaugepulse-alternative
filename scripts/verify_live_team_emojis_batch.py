"""Rate-limit-friendly live team-card emoji verification."""

import re
import sqlite3

from leaguebot.config import Config
from scripts.verify_live_rollout import get_json


config = Config.from_env()
connection = sqlite3.connect(config.database_path)
connection.row_factory = sqlite3.Row
cards = connection.execute(
    "SELECT team_name,channel_id,message_id FROM open_team_cards ORDER BY team_name"
).fetchall()
messages = get_json(config.token, f"/channels/{cards[0]['channel_id']}/messages?limit=100")
by_id = {str(message["id"]): message for message in messages}
rendered, missing = [], []
for card in cards:
    embed = (by_id.get(str(card["message_id"]), {}).get("embeds") or [{}])[0]
    title = embed.get("title") or ""
    (rendered if re.search(r"<a?:[A-Za-z0-9_]+:\d+>", title) else missing).append(
        card["team_name"]
    )
print("cards_with_custom_emoji", len(rendered))
print("cards_without_custom_emoji", len(missing))
print("missing_cards", missing)

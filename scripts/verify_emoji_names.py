"""Read-only comparison of guild emoji names and configured franchise names."""

from __future__ import annotations

import sqlite3

from leaguebot.config import Config
from scripts.verify_live_rollout import get_json


config = Config.from_env()
connection = sqlite3.connect(config.database_path)
connection.row_factory = sqlite3.Row
guild_id = connection.execute(
    "SELECT guild_id FROM guild_settings ORDER BY guild_id LIMIT 1"
).fetchone()["guild_id"]
emojis = get_json(config.token, f"/guilds/{guild_id}/emojis")
names = sorted(emoji["name"] for emoji in emojis)
print("guild_emoji_count", len(names))
print("cardinal_candidates", [name for name in names if "card" in name.casefold()])

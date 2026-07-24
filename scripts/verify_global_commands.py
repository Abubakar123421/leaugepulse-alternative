"""Print safe global slash-command rollout details."""

from __future__ import annotations

from scripts.verify_live_rollout import get_json
from leaguebot.config import Config


config = Config.from_env()
bot = get_json(config.token, "/users/@me")
commands = get_json(config.token, f"/applications/{bot['id']}/commands")
names = sorted(command["name"] for command in commands)
required = {
    "gameoftheweek",
    "weekmvp",
    "seasonaward",
    "seasonawards",
    "syncteamemojis",
    "setteamemoji",
}
print("required_present", sorted(required.intersection(names)))
print("required_missing", sorted(required.difference(names)))
print("aipreview_retired", "aipreview" not in names)

"""Read-only live checks for the pre-launch rollout. Never prints credentials."""

from __future__ import annotations

import json
import sqlite3
import urllib.request

from leaguebot.config import Config


def get_json(token: str, path: str):
    request = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {token}", "User-Agent": "JanthoBot rollout check"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    config = Config.from_env()
    connection = sqlite3.connect(config.database_path)
    connection.row_factory = sqlite3.Row
    guild_id = connection.execute(
        "SELECT guild_id FROM guild_settings ORDER BY guild_id LIMIT 1"
    ).fetchone()["guild_id"]
    bot = get_json(config.token, "/users/@me")
    commands = get_json(
        config.token, f"/applications/{bot['id']}/guilds/{guild_id}/commands"
    )
    command_names = sorted(command["name"] for command in commands)
    required = {
        "gameoftheweek",
        "weekmvp",
        "seasonaward",
        "seasonawards",
        "syncteamemojis",
        "setteamemoji",
    }
    week_one = connection.execute(
        """SELECT channel_id,message_id FROM matchups
           WHERE guild_id=? AND week=1 AND channel_id IS NOT NULL
           ORDER BY id LIMIT 1""",
        (guild_id,),
    ).fetchone()
    pinned = get_json(config.token, f"/channels/{week_one['channel_id']}/pins")
    cards = connection.execute(
        """SELECT channel_id,message_id FROM open_team_cards
           WHERE guild_id=? ORDER BY team_name LIMIT 1""",
        (guild_id,),
    ).fetchone()
    card = get_json(
        config.token, f"/channels/{cards['channel_id']}/messages/{cards['message_id']}"
    )
    labels = [
        component.get("label")
        for row in card.get("components", [])
        for component in row.get("components", [])
        if component.get("label")
    ]
    print(
        json.dumps(
            {
                "required_commands_present": sorted(required.intersection(command_names)),
                "missing_required_commands": sorted(required.difference(command_names)),
                "aipreview_retired": "aipreview" not in command_names,
                "week_one_channels": connection.execute(
                    "SELECT COUNT(1) FROM matchups WHERE guild_id=? AND week=1 AND channel_id IS NOT NULL",
                    (guild_id,),
                ).fetchone()[0],
                "matchup_message_is_pinned": any(
                    str(message["id"]) == str(week_one["message_id"]) for message in pinned
                ),
                "open_team_card_buttons": labels,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

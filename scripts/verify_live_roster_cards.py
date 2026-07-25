"""Rate-limit-friendly verification of all live Open Teams card controls."""

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
roster_only: list[str] = []
claim_buttons: list[str] = []
unexpected: dict[str, list[str]] = {}
for card in cards:
    message = by_id.get(str(card["message_id"]), {})
    labels = [
        component.get("label")
        for row in message.get("components", [])
        for component in row.get("components", [])
        if component.get("label")
    ]
    if labels == ["View Team"]:
        roster_only.append(card["team_name"])
    else:
        unexpected[card["team_name"]] = labels
    if "Claim Team" in labels:
        claim_buttons.append(card["team_name"])

print("roster_only_cards", len(roster_only))
print("cards_with_claim_button", claim_buttons)
print("unexpected_cards", unexpected)

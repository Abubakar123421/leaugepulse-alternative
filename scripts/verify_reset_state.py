"""Read-only reset-state acceptance checks."""

from __future__ import annotations

import sqlite3

from leaguebot.config import Config


config = Config.from_env()
connection = sqlite3.connect(config.database_path)
checks = {
    "profiles": "SELECT COUNT(1) FROM profiles",
    "waiting_fixtures": "SELECT COUNT(1) FROM matchups WHERE status='waiting'",
    "pending_results": """SELECT COUNT(1) FROM matchups
                          WHERE result_submitted_by IS NOT NULL
                             OR result_evidence_url IS NOT NULL""",
    "assigned_matchups": """SELECT COUNT(1) FROM matchups
                            WHERE away_user_id IS NOT NULL
                               OR home_user_id IS NOT NULL""",
    "matchup_prompts": "SELECT COUNT(1) FROM matchup_prompts",
    "reminder_deliveries": "SELECT COUNT(1) FROM reminder_deliveries",
    "matchup_cases": "SELECT COUNT(1) FROM matchup_cases",
    "participants": "SELECT COUNT(1) FROM season_participants",
    "career_profiles": "SELECT COUNT(1) FROM career_profiles",
    "open_teams": "SELECT COUNT(1) FROM open_rosters",
    "week_1_channels": """SELECT COUNT(1) FROM matchups
                          WHERE week=1 AND channel_id IS NOT NULL""",
    "later_week_channels": """SELECT COUNT(1) FROM matchups
                              WHERE week<>1 AND channel_id IS NOT NULL""",
    "emoji_names": "SELECT COUNT(1) FROM franchises WHERE emoji_name IS NOT NULL",
}
for label, query in checks.items():
    print(label, connection.execute(query).fetchone()[0])

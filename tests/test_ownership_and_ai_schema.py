import asyncio

import pytest

from leaguebot.db import Database
from leaguebot.ownership import OwnershipError, claim_team, initialize_open_teams


@pytest.mark.asyncio
async def test_simultaneous_claims_have_one_winner(tmp_path):
    db = Database(tmp_path / "claims.sqlite3")
    await db.initialize()
    await db.ensure_guild(1)
    await initialize_open_teams(db, 1, "1")

    results = await asyncio.gather(
        claim_team(db, 1, "1", 100, "49ers"),
        claim_team(db, 1, "1", 200, "49ERS"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, OwnershipError) for item in results) == 1
    profiles = await db.fetchall("SELECT * FROM profiles WHERE guild_id=1")
    assert len(profiles) == 1
    assert profiles[0]["assignment_source"] == "self_claim"


@pytest.mark.asyncio
async def test_ai_and_open_panel_migrations_are_additive(tmp_path):
    db = Database(tmp_path / "migrations.sqlite3")
    await db.initialize()
    await db.update_settings(
        5, open_teams_message_id=99, ai_enabled=0, ai_style="documentary"
    )
    settings = await db.settings(5)
    assert settings["open_teams_message_id"] == 99
    assert settings["ai_enabled"] == 0
    assert settings["ai_style"] == "documentary"
    columns = await db.fetchall("PRAGMA table_info(ai_jobs)")
    assert {row["name"] for row in columns} >= {"source_key", "prompt_version", "posted_message_id"}

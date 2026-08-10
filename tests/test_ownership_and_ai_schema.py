import asyncio

import pytest

from leaguebot.db import Database
from leaguebot.ownership import (
    OwnershipError,
    assign_team_directly,
    claim_team,
    decide_team_claim,
    initialize_open_teams,
    request_team_claim,
)


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


@pytest.mark.asyncio
async def test_button_claim_is_pending_and_denial_reopens_team(tmp_path):
    db = Database(tmp_path / "pending-claim.sqlite3")
    await db.initialize()
    await db.ensure_guild(1)
    await initialize_open_teams(db, 1, "1")

    claim = await request_team_claim(db, 1, "1", 100, "49ers")
    profile = await db.fetchone("SELECT * FROM profiles WHERE guild_id=1 AND user_id=100")
    assert profile["approved"] == 0
    assert claim.team_name == "49ers"
    with pytest.raises(OwnershipError, match="pending claim"):
        await request_team_claim(db, 1, "1", 100, "Bears")
    with pytest.raises(OwnershipError, match="owns or has a pending"):
        await request_team_claim(db, 1, "1", 200, "49ers")

    await decide_team_claim(db, 1, "1", 100, approved=False, decided_by=999)
    assert await db.fetchone("SELECT * FROM profiles WHERE guild_id=1 AND user_id=100") is None
    reopened = await db.fetchone(
        "SELECT 1 FROM open_rosters WHERE guild_id=1 AND season='1' AND team_name='49ers'"
    )
    assert reopened is not None

    await request_team_claim(db, 1, "1", 200, "49ers")
    approved = await decide_team_claim(
        db, 1, "1", 200, approved=True, decided_by=999
    )
    assert approved.user_id == 200
    owner = await db.fetchone("SELECT * FROM profiles WHERE guild_id=1 AND user_id=200")
    assert owner["approved"] == 1
    assert owner["approved_by"] == 999
    assert await db.fetchone(
        "SELECT 1 FROM open_rosters WHERE guild_id=1 AND season='1' AND team_name='49ers'"
    ) is None


@pytest.mark.asyncio
async def test_direct_reassignment_preserves_completed_games_and_updates_future(tmp_path):
    db = Database(tmp_path / "reassign.sqlite3")
    await db.initialize()
    await db.ensure_guild(1)
    await initialize_open_teams(db, 1, "1")
    await claim_team(db, 1, "1", 100, "49ers")
    for values in [
        (1, "done", "49ers", "Bears", "complete", "now", "now"),
        (2, "future", "49ers", "Bears", "waiting", "now", "now"),
    ]:
        await db.execute(
            """INSERT INTO matchups
               (guild_id,season,week,external_key,away_team,home_team,away_user_id,
                status,created_at,updated_at)
               VALUES (1,'1',?,?,?,?,100,?,?,?)""",
            values,
        )

    assignment = await assign_team_directly(
        db, 1, "1", 200, "49ers", assigned_by=999, replace_existing=True
    )
    assert assignment.previous_user_id == 100
    completed = await db.fetchone("SELECT away_user_id FROM matchups WHERE external_key='done'")
    future = await db.fetchone("SELECT away_user_id FROM matchups WHERE external_key='future'")
    assert completed["away_user_id"] == 100
    assert future["away_user_id"] == 200
    assert await db.fetchone("SELECT * FROM profiles WHERE guild_id=1 AND user_id=100") is None
    replacement = await db.fetchone("SELECT * FROM profiles WHERE guild_id=1 AND user_id=200")
    assert replacement["approved"] == 1
    assert replacement["assignment_source"] == "commissioner_direct"

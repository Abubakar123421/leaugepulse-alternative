import pytest

from leaguebot.availability_ui import _claim_case, create_or_update_case, latest_open_case
from leaguebot.db import Database


@pytest.mark.asyncio
async def test_repeat_availability_request_updates_one_open_case(tmp_path):
    db = Database(tmp_path / "cases.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,status,created_at,updated_at)
           VALUES (1,'1',1,'game','Away','Home',10,20,'scheduled','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    first = await create_or_update_case(
        db,
        matchup=matchup,
        opened_by=10,
        kind="availability",
        reason="My availability changed unexpectedly.",
    )
    second = await create_or_update_case(
        db,
        matchup=matchup,
        opened_by=10,
        kind="availability",
        reason="Updated availability and a clearer explanation.",
        requested_deadline_at="2026-08-18T21:00:00+00:00",
    )

    assert first == second
    case = await latest_open_case(db, matchup_id)
    assert case["reason"].startswith("Updated")
    assert case["requested_deadline_at"] == "2026-08-18T21:00:00+00:00"
    count = await db.fetchone(
        "SELECT COUNT(*) AS total FROM matchup_cases WHERE matchup_id=?",
        (matchup_id,),
    )
    assert count["total"] == 1


@pytest.mark.asyncio
async def test_only_one_commissioner_can_claim_a_case(tmp_path):
    db = Database(tmp_path / "claim.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,status,created_at,updated_at)
           VALUES (1,'1',1,'claim','Away','Home','waiting','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    case_id = await create_or_update_case(
        db, matchup=matchup, opened_by=10, kind="issue", reason="Need help."
    )

    assert await _claim_case(db, case_id)
    assert not await _claim_case(db, case_id)
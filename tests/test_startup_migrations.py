import pytest

from leaguebot.db import Database
from leaguebot.startup_migrations import backfill_active_leagues


@pytest.mark.asyncio
async def test_startup_backfill_is_idempotent_and_makes_legacy_issue_actionable(tmp_path):
    db = Database(tmp_path / "startup.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="1")
    await db.execute(
        """INSERT INTO profiles
           (guild_id,user_id,team_name,approved,updated_at)
           VALUES (1,10,'Away',1,'now'),(1,20,'Home',1,'now')"""
    )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,away_score,home_score,status,
            created_at,updated_at)
           VALUES
           (1,'1',1,'final','Away','Home',10,20,24,17,'complete','now','now'),
           (1,'1',2,'issue','Away','Home',10,20,NULL,NULL,'issue_reported','now','now')"""
    )
    await db.execute(
        "UPDATE matchups SET issue_text='Opponent unavailable' WHERE external_key='issue'"
    )

    await backfill_active_leagues(db)
    await backfill_active_leagues(db)

    winner = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=10"
    )
    assert (winner["games"], winner["wins"], winner["xp"]) == (1, 1, 100)
    events = await db.fetchone("SELECT COUNT(*) AS total FROM career_events")
    assert events["total"] == 2
    cases = await db.fetchone("SELECT COUNT(*) AS total FROM matchup_cases")
    assert cases["total"] == 1

import pytest

from leaguebot.db import Database
from leaguebot.progression import (
    level_for_xp,
    next_level_xp,
    record_matchup_progress,
    win_ratio,
)


def test_level_and_ratio_boundaries():
    assert level_for_xp(0) == 1
    assert level_for_xp(199) == 1
    assert level_for_xp(200) == 2
    assert next_level_xp(2) == 800
    assert win_ratio(0, 0) == 0
    assert win_ratio(3, 1) == 75


@pytest.mark.asyncio
async def test_completed_game_awards_career_once(tmp_path):
    db = Database(tmp_path / "progress.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,away_score,home_score,status,
            created_at,updated_at)
           VALUES (1,'1',1,'game-1','Away','Home',10,20,28,14,
                   'complete','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    async with db.connect() as conn:
        await record_matchup_progress(conn, matchup, "complete")
        await record_matchup_progress(conn, matchup, "complete")
        await conn.commit()

    winner = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=10"
    )
    loser = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=20"
    )
    assert (winner["games"], winner["wins"], winner["xp"]) == (1, 1, 100)
    assert (loser["games"], loser["losses"], loser["xp"]) == (1, 1, 50)


@pytest.mark.asyncio
async def test_force_win_is_not_counted_as_played_game(tmp_path):
    db = Database(tmp_path / "force.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,status,created_at,updated_at)
           VALUES (1,'1',1,'game-2','Away','Home',10,20,
                   'force_home','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    async with db.connect() as conn:
        await record_matchup_progress(conn, matchup, "force_home")
        await conn.commit()

    away = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=10"
    )
    home = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=20"
    )
    assert (away["games"], away["losses"], away["forfeits"], away["xp"]) == (
        0,
        1,
        1,
        0,
    )
    assert (home["games"], home["wins"], home["force_wins"], home["xp"]) == (
        0,
        1,
        1,
        60,
    )

import pytest

from leaguebot.db import Database
from leaguebot.season_lifecycle import archive_season, season_close_preview
from leaguebot.awards import AWARD_CATEGORIES, set_season_award


async def publish_test_awards(db, guild_id: int, season: str) -> None:
    for key, label in AWARD_CATEGORIES:
        await set_season_award(
            db, guild_id, season, key, f"{label} Winner", "Test selection", 99
        )
    await db.execute(
        """UPDATE season_award_summaries SET status='published',
           approved_by=99,approved_at='now',updated_at='now'
           WHERE guild_id=? AND season=?""",
        (guild_id, season),
    )

@pytest.mark.asyncio
async def test_season_archive_preserves_history_and_purges_operations(tmp_path):
    db = Database(tmp_path / "season.sqlite3")
    await db.initialize()
    await db.update_settings(
        1,
        league_name="Test League",
        game="Madden 26",
        season="1",
        category_id=999,
    )
    await db.execute(
        """INSERT INTO profiles
           (guild_id,user_id,team_name,approved,updated_at)
           VALUES (1,10,'Away',1,'now'),(1,20,'Home',1,'now')"""
    )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,away_score,home_score,status,
            result_evidence_url,result_reviewed_at,created_at,updated_at)
           VALUES (1,'1',1,'final','Away','Home',10,20,31,21,'complete',
                   'https://temporary.example/proof.png','now','now','now')"""
    )
    await db.execute(
        """INSERT INTO transactions
           (guild_id,season,kind,user_id,details,created_at)
           VALUES (1,'1','trade',10,'temporary','now')"""
    )
    await db.audit(1, 10, "temporary_season_action")
    await db.audit(2, 20, "other_server_action")

    preview = await season_close_preview(db, 1, "1")
    assert preview.can_close
    await publish_test_awards(db, 1, "1")

    result = await archive_season(
        db,
        guild_id=1,
        season="1",
        new_season="2",
        actor_id=99,
        champion_user_id=10,
    )

    assert result.games_archived == 1
    assert result.participants_preserved == 2
    assert await db.fetchone("SELECT * FROM matchups WHERE guild_id=1") is None
    assert await db.fetchone("SELECT * FROM profiles WHERE guild_id=1") is None
    assert await db.fetchone("SELECT * FROM transactions WHERE guild_id=1") is None
    assert await db.fetchone("SELECT * FROM audit_logs WHERE guild_id=1") is None
    assert await db.fetchone("SELECT * FROM audit_logs WHERE guild_id=2") is not None
    history = await db.fetchone("SELECT * FROM game_history WHERE guild_id=1")
    assert (history["away_score"], history["home_score"]) == (31, 21)
    assert "evidence" not in history.keys()
    archive = await db.fetchone("SELECT * FROM season_archives WHERE guild_id=1")
    assert (archive["champion_user_id"], archive["champion_team"]) == (10, "Away")
    career = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=10"
    )
    assert (career["wins"], career["championships"], career["xp"]) == (1, 1, 600)
    assert (await db.settings(1))["season"] == "2"
    assert (await db.settings(1))["current_week"] == 1


@pytest.mark.asyncio
async def test_unresolved_season_cannot_be_archived(tmp_path):
    db = Database(tmp_path / "unresolved.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="1")
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,status,
            created_at,updated_at)
           VALUES (1,'1',1,'open','Away','Home','waiting','now','now')"""
    )

    preview = await season_close_preview(db, 1, "1")
    assert not preview.can_close
    with pytest.raises(ValueError, match="unresolved"):
        await archive_season(
            db,
            guild_id=1,
            season="1",
            new_season="2",
            actor_id=99,
        )
    assert await db.fetchone("SELECT * FROM matchups WHERE guild_id=1")
    assert (await db.settings(1))["season"] == "1"

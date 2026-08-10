import pytest

from leaguebot.db import Database
from leaguebot.season_lifecycle import (
    archive_season,
    force_delete_active_season,
    reset_active_season_test_data,
    season_close_preview,
    season_force_delete_preview,
    season_test_reset_preview,
)
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

@pytest.mark.asyncio
async def test_force_delete_active_test_season_is_scoped_and_reverses_progress(tmp_path):
    db = Database(tmp_path / "force-delete.sqlite3")
    await db.initialize()
    await db.update_settings(
        1,
        league_name="Demo League",
        game="Madden 26",
        season="Demo",
        final_scores_channel_id=111,
        audit_channel_id=222,
        commissioner_role_id=333,
        open_teams_message_id=444,
        category_id=555,
        matchup_category_id=555,
    )
    await db.update_settings(2, season="Demo")

    # Permanent stats already include one active Demo result plus older history.
    await db.execute(
        """INSERT INTO career_profiles
           (guild_id,user_id,games,wins,losses,points_for,points_against,xp,
            created_at,updated_at)
           VALUES (1,10,3,2,1,70,50,300,'now','now')"""
    )
    await db.execute(
        """INSERT INTO season_participants
           (guild_id,season,user_id,team_name,games,wins,points_for,
            points_against,xp,joined_at,updated_at)
           VALUES (1,'Demo',10,'Bears',1,1,24,10,100,'now','now')"""
    )
    await db.execute(
        """INSERT INTO career_events
           (guild_id,season,source_key,user_id,event_type,xp,created_at)
           VALUES (1,'Demo','1:demo-game',10,'complete',100,'now')"""
    )
    await db.execute(
        """INSERT INTO franchises
           (guild_id,season,external_team_id,team_name,abbreviation,imported_at)
           VALUES (1,'Demo','CHI','Bears','CHI','now'),
                  (2,'Demo','GB','Packers','GB','now')"""
    )
    await db.execute(
        """INSERT INTO roster_players
           (guild_id,season,external_player_id,external_team_id,team_name,
            full_name,position,imported_at)
           VALUES (1,'Demo','p1','CHI','Bears','Test Player','QB','now')"""
    )
    await db.execute(
        """INSERT INTO profiles
           (guild_id,user_id,team_name,approved,updated_at)
           VALUES (1,10,'Bears',1,'now'),(2,20,'Packers',1,'now')"""
    )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,status,created_at,updated_at)
           VALUES (1,'Demo',1,'demo-game','Bears','Packers',10,'waiting','now','now'),
                  (2,'Demo',1,'other-game','Packers','Bears',20,'waiting','now','now')"""
    )
    await db.execute(
        """INSERT INTO open_rosters
           (guild_id,season,team_name,updated_at) VALUES (1,'Demo','Bears','now')"""
    )
    await db.execute(
        """INSERT INTO season_archives
           (guild_id,season,league_name,game,total_games,cleanup_status,
            archived_by,archived_at)
           VALUES (1,'Archived','Demo League','Madden 26',1,'complete',99,'now')"""
    )
    await db.audit(1, 10, "demo_action")
    await db.audit(2, 20, "other_guild_action")

    preview = await season_force_delete_preview(db, 1, "Demo")
    assert (preview.matchups, preview.franchises, preview.roster_players, preview.owners) == (
        1, 1, 1, 1
    )

    result = await force_delete_active_season(
        db,
        guild_id=1,
        season="Demo",
        new_season="Launch",
        actor_id=99,
    )
    assert (result.matchups_deleted, result.roster_players_deleted) == (1, 1)

    settings = await db.settings(1)
    assert settings["season"] == "Launch"
    assert settings["current_week"] == 1
    assert settings["final_scores_channel_id"] == 111
    assert settings["audit_channel_id"] == 222
    assert settings["commissioner_role_id"] == 333
    assert settings["open_teams_message_id"] is None
    assert settings["category_id"] is None

    for table in (
        "matchups", "franchises", "roster_players", "profiles",
        "open_rosters", "season_participants", "career_events",
    ):
        assert await db.fetchone(
            f"SELECT 1 FROM {table} WHERE guild_id=1 AND "
            + ("1=1" if table == "profiles" else "season='Demo'")
        ) is None

    career = await db.fetchone(
        "SELECT * FROM career_profiles WHERE guild_id=1 AND user_id=10"
    )
    assert (career["games"], career["wins"], career["losses"], career["xp"]) == (
        2, 1, 1, 200
    )
    assert await db.fetchone(
        "SELECT 1 FROM season_archives WHERE guild_id=1 AND season='Archived'"
    )
    assert await db.fetchone(
        "SELECT 1 FROM matchups WHERE guild_id=2 AND season='Demo'"
    )
    assert await db.fetchone(
        "SELECT 1 FROM franchises WHERE guild_id=2 AND season='Demo'"
    )
    assert await db.fetchone(
        "SELECT 1 FROM profiles WHERE guild_id=2 AND user_id=20"
    )
    assert await db.fetchone(
        "SELECT 1 FROM audit_logs WHERE guild_id=2 AND action='other_guild_action'"
    )
    audit = await db.fetchone(
        "SELECT * FROM audit_logs WHERE guild_id=1"
    )
    assert audit["action"] == "season_force_deleted"


@pytest.mark.asyncio
async def test_safe_test_reset_preserves_completed_history_and_resets_unfinished(tmp_path):
    db = Database(tmp_path / "safe-reset.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="Demo", current_week=3)
    await db.execute(
        """INSERT INTO career_profiles
           (guild_id,user_id,games,wins,xp,created_at,updated_at)
           VALUES (1,10,1,1,100,'now','now')"""
    )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,away_user_id,
            home_user_id,away_score,home_score,status,channel_id,message_id,
            result_submitted_by,result_evidence_url,created_at,updated_at)
           VALUES
           (1,'Demo',1,'final','49ers','Bears',10,20,24,17,'complete',111,222,
            10,'https://example.test/final.png','now','now'),
           (1,'Demo',3,'pending','Cardinals','Rams',30,40,21,20,'result_pending',333,444,
            30,'https://example.test/pending.png','now','now')"""
    )
    pending = await db.fetchone("SELECT id FROM matchups WHERE external_key='pending'")
    await db.execute(
        """INSERT INTO matchup_prompts
           (matchup_id,user_id,kind,expires_at,created_at)
           VALUES (?,30,'result','later','now')""",
        (pending["id"],),
    )
    await db.execute(
        """INSERT INTO reminder_deliveries (matchup_id,milestone,delivered_at)
           VALUES (?,'24h','now')""",
        (pending["id"],),
    )
    await db.execute(
        """INSERT INTO matchup_cases
           (matchup_id,guild_id,season,opened_by,kind,reason,created_at)
           VALUES (?,1,'Demo',30,'dispute','test case','now')""",
        (pending["id"],),
    )
    await db.execute(
        """INSERT INTO week_categories
           (guild_id,season,week,category_id,created_at)
           VALUES (1,'Demo',3,999,'now')"""
    )

    preview = await season_test_reset_preview(db, 1, "Demo")
    assert (preview.unfinished_matchups, preview.completed_matchups) == (1, 1)
    result = await reset_active_season_test_data(
        db, guild_id=1, season="Demo", actor_id=99
    )
    assert (result.matchups_reset, result.completed_matchups_preserved) == (1, 1)

    final = await db.fetchone("SELECT * FROM matchups WHERE external_key='final'")
    assert (final["status"], final["away_score"], final["home_score"]) == (
        "complete", 24, 17
    )
    assert final["final_score_message_id"] is None
    assert final["channel_id"] is None
    reset = await db.fetchone("SELECT * FROM matchups WHERE external_key='pending'")
    assert reset["status"] == "waiting"
    assert reset["away_score"] is None
    assert reset["result_evidence_url"] is None
    assert reset["channel_id"] is None
    assert await db.fetchone("SELECT 1 FROM matchup_prompts") is None
    assert await db.fetchone("SELECT 1 FROM reminder_deliveries") is None
    assert await db.fetchone("SELECT 1 FROM matchup_cases") is None
    assert await db.fetchone("SELECT 1 FROM week_categories") is None
    career = await db.fetchone("SELECT * FROM career_profiles WHERE guild_id=1")
    assert (career["games"], career["wins"], career["xp"]) == (1, 1, 100)
    settings = await db.settings(1)
    assert settings["current_week"] == 1
    audit = await db.fetchone(
        "SELECT * FROM audit_logs WHERE guild_id=1 AND action='season_test_data_reset'"
    )
    assert audit["actor_id"] == 99

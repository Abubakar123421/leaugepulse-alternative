import aiosqlite
import pytest
from types import SimpleNamespace

from leaguebot.channel_workflow import MatchupDisputeView
from leaguebot.db import Database, MATCHUP_RESULT_COLUMNS
from leaguebot.result_ui import (
    CommissionerResultReviewView,
    MatchupScoreSubmissionView,
    OpponentResultDecisionView,
    ResultSubmissionModal,
    _attachment_url,
    _apply_result_decision,
)


@pytest.mark.asyncio
async def test_existing_matchups_table_gets_result_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """CREATE TABLE matchups (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                season TEXT NOT NULL,
                week INTEGER NOT NULL,
                deadline_at TEXT,
                status TEXT NOT NULL
            )"""
        )
        await conn.commit()

    db = Database(path)
    await db.initialize()
    rows = await db.fetchall("PRAGMA table_info(matchups)")
    names = {row["name"] for row in rows}

    assert MATCHUP_RESULT_COLUMNS.keys() <= names


def test_result_decision_views_are_persistent():
    opponent = OpponentResultDecisionView(42)
    commissioner = CommissionerResultReviewView(42)

    assert opponent.timeout is None
    assert commissioner.timeout is None
    assert {item.custom_id for item in opponent.children} == {
        "leaguebot:result:confirm:42",
        "leaguebot:result:dispute:42",
    }
    assert {item.custom_id for item in commissioner.children} == {
        "leaguebot:result:staff:approve:42",
        "leaguebot:result:staff:reject:42",
        "leaguebot:result:staff:edit:42",
    }

def test_matchup_dispute_control_is_persistent():
    view = MatchupDisputeView(42)

    assert view.timeout is None
    assert {item.custom_id for item in view.children} == {
        "leaguebot:matchup:dispute:42",
    }


def test_versioned_result_controls_cannot_target_newer_submissions():
    versioned_opponent = OpponentResultDecisionView(42, 3)
    versioned_commissioner = CommissionerResultReviewView(42, 3)
    assert {item.custom_id for item in versioned_opponent.children} == {
        "leaguebot:result:confirm:42:3",
        "leaguebot:result:dispute:42:3",
    }
    assert {item.custom_id for item in versioned_commissioner.children} == {
        "leaguebot:result:staff:approve:42:3",
        "leaguebot:result:staff:reject:42:3",
        "leaguebot:result:staff:edit:42:3",
    }


def test_player_score_submission_is_one_persistent_button_with_team_labels():
    view = MatchupScoreSubmissionView(42)
    modal = ResultSubmissionModal(
        None,
        {"id": 42, "away_team": "Vikings", "home_team": "49ers"},
    )

    assert view.timeout is None
    assert len(view.children) == 1
    assert view.children[0].custom_id == "leaguebot:matchup:submit:42"
    assert view.children[0].item.label == "Game Complete / Submit Score"
    assert modal.children[0].text == "Vikings score"
    assert modal.children[1].text == "49ers score"

@pytest.mark.asyncio
async def test_attachment_url_refetches_a_new_audit_message():
    message = SimpleNamespace(id=99, attachments=[])
    refreshed = SimpleNamespace(
        attachments=[SimpleNamespace(url="https://cdn.example/result.png")]
    )

    class Channel:
        async def fetch_message(self, message_id):
            assert message_id == 99
            return refreshed

    assert (
        await _attachment_url(Channel(), message, "https://temporary.example/upload")
        == "https://cdn.example/result.png"
    )

@pytest.mark.asyncio
async def test_approval_updates_standings_only_once(tmp_path):
    db = Database(tmp_path / "results.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_score,home_score,status,result_submitted_by,
            created_at,updated_at)
           VALUES (1,'1',1,'w1-a-b','Away','Home',24,17,'result_pending',10,
                   'now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    assert await _apply_result_decision(db, matchup, "approve", 99) is True
    assert await _apply_result_decision(db, matchup, "approve", 99) is False

    updated = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    away = await db.fetchone("SELECT * FROM teams WHERE name='Away'")
    home = await db.fetchone("SELECT * FROM teams WHERE name='Home'")
    assert updated["status"] == "complete"
    assert (away["wins"], away["losses"]) == (1, 0)
    assert (home["wins"], home["losses"]) == (0, 1)

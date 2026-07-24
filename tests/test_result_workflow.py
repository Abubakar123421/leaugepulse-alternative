import aiosqlite
import pytest
from types import SimpleNamespace

from leaguebot.db import Database, MATCHUP_RESULT_COLUMNS
from leaguebot.result_ui import (
    CommissionerResultReviewView,
    OpponentResultDecisionView,
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
        "leaguebot:result:staff:evidence:42",
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
        "leaguebot:result:staff:evidence:42:3",
    }

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
            away_score,home_score,status,result_submitted_by,result_evidence_url,
            created_at,updated_at)
           VALUES (1,'1',1,'w1-a-b','Away','Home',24,17,'result_pending',10,
                   'https://cdn.example/proof.png','now','now')"""
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

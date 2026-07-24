from types import SimpleNamespace

import pytest

from leaguebot.awards import AWARD_CATEGORIES, awards_ready, set_season_award
from leaguebot.db import Database
from leaguebot.open_teams_ui import ViewRosterCardButton
from leaguebot.team_emojis import sync_team_emojis, team_emoji
from leaguebot.weekly_content import weekly_facts


class FakeEmoji:
    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return f"<:{self.name}:123>"


@pytest.mark.asyncio
async def test_team_emojis_resolve_by_name_and_support_uploaded_typos(tmp_path):
    db = Database(tmp_path / "emoji.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="1")
    await db.execute(
        """INSERT INTO franchises
           (guild_id,season,external_team_id,team_name,abbreviation,sort_order,imported_at)
           VALUES
           (1,'1','cin','Bengals','CIN',1,'now'),
           (1,'1','sf','49ers','SF',2,'now'),
           (1,'1','dal','Dragons','DAL',3,'now')"""
    )
    guild = SimpleNamespace(
        id=1,
        emojis=[
            FakeEmoji("CinncinatiBengals"),
            FakeEmoji("SanFrancisco49ners"),
            FakeEmoji("DallasCowboys"),
        ],
    )
    matched, missing = await sync_team_emojis(db, guild, "1")
    assert matched == 3
    assert missing == []
    assert (await team_emoji(db, guild, "1", "Dragons")).name == "DallasCowboys"


@pytest.mark.asyncio
async def test_weekly_recap_facts_are_deterministic_and_allow_mvp_placeholder(tmp_path):
    db = Database(tmp_path / "weekly.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="1")
    for index, (team, abbr) in enumerate(
        [("Bills", "BUF"), ("Jets", "NYJ"), ("Bears", "CHI"), ("Packers", "GB")]
    ):
        await db.execute(
            """INSERT INTO franchises
               (guild_id,season,external_team_id,team_name,abbreviation,sort_order,imported_at)
               VALUES (1,'1',?,?,?,?,'now')""",
            (str(index), team, abbr, index),
        )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_score,home_score,status,created_at,updated_at)
           VALUES
           (1,'1',1,'a','Bills','Jets',21,20,'complete','now','now'),
           (1,'1',1,'b','Bears','Packers',7,35,'complete','now','now')"""
    )
    facts = await weekly_facts(db, 1, "1", 1)
    assert facts["best_game"]["away_team"] == "Bills"
    assert facts["biggest_blowout"]["home_team"] == "Packers"
    assert facts["mvp"] is None
    assert facts["unresolved"] == 0
    assert len(facts["top_five"]) == 4


@pytest.mark.asyncio
async def test_all_awards_must_be_published_before_season_close(tmp_path):
    db = Database(tmp_path / "awards.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="1")
    assert not await awards_ready(db, 1, "1")
    for key, label in AWARD_CATEGORIES:
        await set_season_award(db, 1, "1", key, label, "Selected", 99)
    assert not await awards_ready(db, 1, "1")
    await db.execute(
        """UPDATE season_award_summaries SET status='published',
           approved_by=99,approved_at='now' WHERE guild_id=1 AND season='1'"""
    )
    assert await awards_ready(db, 1, "1")


def test_open_team_card_uses_view_team_label():
    item = ViewRosterCardButton(1, "team")
    assert item.item.label == "View Team"

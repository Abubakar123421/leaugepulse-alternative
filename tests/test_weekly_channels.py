import pytest

from leaguebot.db import Database
from leaguebot.channel_workflow import (
    CONFIRM,
    COUNTER,
    DISPUTE,
    REACTIONS,
    _valid_action_message,
    matchup_channel_embed,
)
from leaguebot.registration import registration_state
from leaguebot.team_roles import MADDEN_TEAMS


def test_madden_team_catalog_has_all_32_unique_teams():
    assert len(MADDEN_TEAMS) == 32
    assert len({team.casefold() for team in MADDEN_TEAMS}) == 32
    assert {"49ers", "Cowboys", "Commanders", "Vikings"} <= set(MADDEN_TEAMS)


@pytest.mark.asyncio
async def test_registration_works_before_the_first_week_is_imported(tmp_path):
    db = Database(tmp_path / "weekly.sqlite3")
    await db.initialize()

    state = await registration_state(db, guild_id=123, season="1")

    assert len(state.all_teams) == 32
    assert state.canonical("cOwBoYs") == "Cowboys"
    assert state.canonical("Not A Team") is None


@pytest.mark.asyncio
async def test_weekly_channel_schema_survives_restart(tmp_path):
    path = tmp_path / "weekly.sqlite3"
    first = Database(path)
    await first.initialize()
    await first.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,channel_id,
            status,created_at,updated_at)
           VALUES (1,'1',1,'game-1','49ers','Cowboys',999,'waiting','now','now')"""
    )
    await first.execute(
        """INSERT INTO week_categories
           (guild_id,season,week,category_id,created_at)
           VALUES (1,'1',1,888,'now')"""
    )

    second = Database(path)
    await second.initialize()

    matchup = await second.fetchone("SELECT channel_id FROM matchups WHERE external_key='game-1'")
    category = await second.fetchone(
        "SELECT category_id FROM week_categories WHERE guild_id=1 AND season='1' AND week=1"
    )
    assert matchup["channel_id"] == 999
    assert category["category_id"] == 888


def test_result_dispute_reaction_is_accepted():
    assert DISPUTE in REACTIONS

def test_proposal_and_result_messages_accept_direct_reactions():
    assert _valid_action_message("schedule_pending", CONFIRM, "@owner proposed Friday at 8 PM")
    assert _valid_action_message("schedule_pending", COUNTER, "@owner proposed Friday at 8 PM")
    assert _valid_action_message("result_pending", CONFIRM, "Score submitted with private evidence")
    assert _valid_action_message("result_pending", DISPUTE, "Score submitted with private evidence")
    assert not _valid_action_message("waiting", CONFIRM, "unrelated bot message")


@pytest.mark.asyncio
async def test_matchup_card_is_clean_and_owner_focused(tmp_path):
    db = Database(tmp_path / "matchup-card.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,status,created_at,updated_at)
           VALUES (1,'1',4,'vikes-niners','Vikings','49ers',10,20,'waiting','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    embed = await matchup_channel_embed(db, matchup, await db.settings(1))

    assert embed.title == "Week 4 Matchup"
    assert "Vikings" in embed.description
    assert "49ers" in embed.description
    assert embed.fields[0].value == "<@10>"
    assert embed.fields[1].value == "<@20>"
    assert all(field.name != "Reactions" for field in embed.fields)

from types import SimpleNamespace

import pytest

from leaguebot.bot import (
    selected_matchup_week,
    send_week_dashboard,
    week_action_autocomplete,
)
from leaguebot.commissioner_ui import (
    CommissionerMatchupActionsView,
    apply_commissioner_outcome,
    reopen_final_matchup,
)
from leaguebot.db import Database


@pytest.mark.asyncio
async def test_week_action_autocomplete_explains_options():
    choices = await week_action_autocomplete(None, "")
    by_value = {choice.value: choice.name for choice in choices}

    assert "screenshot" in by_value["complete"].lower()
    assert "home team" in by_value["force_home"].lower()
    assert "away team" in by_value["force_away"].lower()
    assert "neutral" in by_value["fair_sim"].lower()
    assert "next" in by_value["advance"].lower()


def test_matchup_picker_uses_week_already_entered_in_command():
    interaction = SimpleNamespace(namespace=SimpleNamespace(week=11))

    assert selected_matchup_week(interaction, fallback=1) == 11


def test_matchup_picker_falls_back_to_current_week():
    interaction = SimpleNamespace(namespace=SimpleNamespace())

    assert selected_matchup_week(interaction, fallback=7) == 7


@pytest.mark.asyncio
async def test_player_week_dashboard_omits_empty_view():
    calls = []

    class Followup:
        async def send(self, **kwargs):
            calls.append(kwargs)

    await send_week_dashboard(Followup(), embed="dashboard", view=None)

    assert calls == [{"embed": "dashboard"}]


def test_matchup_actions_name_both_force_win_teams():
    view = CommissionerMatchupActionsView(
        None,
        {"id": 7, "away_team": "49ers", "home_team": "Cowboys", "status": "waiting"},
        actor_id=10,
    )
    labels = {item.label for item in view.children}

    assert "Force Win: 49ers" in labels
    assert "Force Win: Cowboys" in labels


@pytest.mark.asyncio
async def test_force_win_updates_standings_only_once(tmp_path):
    db = Database(tmp_path / "commissioner.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,status,
            created_at,updated_at)
           VALUES (1,'1',1,'w1-a-b','Away','Home','waiting','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    assert await apply_commissioner_outcome(
        db, matchup, "force_home", actor_id=99
    )
    assert not await apply_commissioner_outcome(
        db, matchup, "force_home", actor_id=99
    )

    away = await db.fetchone("SELECT * FROM teams WHERE name='Away'")
    home = await db.fetchone("SELECT * FROM teams WHERE name='Home'")
    assert (away["wins"], away["losses"]) == (0, 1)
    assert (home["wins"], home["losses"]) == (1, 0)

@pytest.mark.asyncio
async def test_reopen_final_reverses_force_win(tmp_path):
    db = Database(tmp_path / "reopen.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,status,created_at,updated_at)
           VALUES (1,'1',1,'reopen','Away','Home',10,20,'waiting','now','now')"""
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    assert await apply_commissioner_outcome(db, matchup, "force_home", 99)
    final = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))

    assert await reopen_final_matchup(db, final, 99)

    reopened = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    away = await db.fetchone("SELECT * FROM teams WHERE name='Away'")
    home = await db.fetchone("SELECT * FROM teams WHERE name='Home'")
    away_career = await db.fetchone(
        "SELECT * FROM career_profiles WHERE user_id=10"
    )
    home_career = await db.fetchone(
        "SELECT * FROM career_profiles WHERE user_id=20"
    )
    assert reopened["status"] == "waiting"
    assert (away["wins"], away["losses"]) == (0, 0)
    assert (home["wins"], home["losses"]) == (0, 0)
    assert (away_career["losses"], away_career["forfeits"]) == (0, 0)
    assert (home_career["wins"], home_career["xp"]) == (0, 0)

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leaguebot import schedule_ui
from leaguebot.db import Database
from leaguebot.schedule_ui import (
    MatchupMarkScheduledButton,
    ScheduleDecisionView,
)


def test_schedule_decision_ids_include_proposal_version():
    view = ScheduleDecisionView(12, 4)
    assert {item.custom_id for item in view.children} == {
        "leaguebot:schedule:accept:12:4",
        "leaguebot:schedule:counter:12:4",
        "leaguebot:schedule:decline:12:4",
    }


def test_mark_scheduled_button_is_persistent():
    button = MatchupMarkScheduledButton(12)
    assert button.custom_id == "leaguebot:matchup:scheduled:12"
    assert button.item.label == "Mark as Scheduled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor_id,is_staff,allowed",
    [(10, False, True), (99, True, True), (30, False, False)],
)
async def test_mark_scheduled_allows_owner_or_commissioner(
    tmp_path, monkeypatch, actor_id, is_staff, allowed
):
    db = Database(tmp_path / "schedule-access.sqlite3")
    await db.initialize()
    matchup_id = await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,status,created_at,updated_at)
           VALUES (1,'1',1,'access','Away','Home',10,20,'waiting','now','now')"""
    )
    monkeypatch.setattr(
        schedule_ui, "is_commissioner", AsyncMock(return_value=is_staff)
    )
    refresh = AsyncMock()
    monkeypatch.setattr(
        "leaguebot.channel_workflow.refresh_matchup_message", refresh
    )
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock())
    client = SimpleNamespace(db=db)
    interaction = SimpleNamespace(
        guild_id=1,
        client=client,
        user=SimpleNamespace(id=actor_id),
        response=response,
        followup=followup,
        channel=channel,
    )

    await MatchupMarkScheduledButton(matchup_id).callback(interaction)

    matchup = await db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
    if allowed:
        assert matchup["status"] == "scheduled"
        assert matchup["scheduled_at"] is None
        channel.send.assert_awaited_once()
        refresh.assert_awaited_once_with(client, db, matchup_id)
    else:
        assert matchup["status"] == "waiting"
        channel.send.assert_not_awaited()
        assert "matchup owners or a Commissioner" in response.send_message.call_args.args[0]

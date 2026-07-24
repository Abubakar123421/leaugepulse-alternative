import pytest

from leaguebot.db import Database


DESTINATION_VALUES = {
    "matchup_category_id": 101,
    "announcements_channel_id": 102,
    "final_scores_channel_id": 103,
    "storyline_channel_id": 104,
    "trade_channel_id": 105,
    "open_teams_channel_id": 106,
    "polls_channel_id": 107,
    "recruiting_channel_id": 108,
    "transactions_channel_id": 109,
    "streams_channel_id": 110,
    "audit_channel_id": 111,
}


@pytest.mark.asyncio
async def test_all_automated_destinations_are_persisted(tmp_path):
    db = Database(tmp_path / "destinations.sqlite3")
    await db.initialize()

    await db.update_settings(1, **DESTINATION_VALUES)
    settings = await db.settings(1)

    for field, expected in DESTINATION_VALUES.items():
        assert settings[field] == expected


@pytest.mark.asyncio
async def test_destination_configuration_is_isolated_by_guild(tmp_path):
    db = Database(tmp_path / "destinations.sqlite3")
    await db.initialize()

    await db.update_settings(1, final_scores_channel_id=1001)
    await db.update_settings(2, final_scores_channel_id=2002)

    assert (await db.settings(1))["final_scores_channel_id"] == 1001
    assert (await db.settings(2))["final_scores_channel_id"] == 2002

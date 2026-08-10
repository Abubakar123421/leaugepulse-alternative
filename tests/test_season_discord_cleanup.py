import pytest

from leaguebot.db import Database
from leaguebot import season_lifecycle


class _FakeTextChannel:
    def __init__(self, channel_id: int, name: str):
        self.id = channel_id
        self.name = name
        self.deleted = False

    async def delete(self, *, reason: str):
        self.deleted = True


class _FakeCategoryChannel:
    def __init__(self, channel_id: int, name: str, channels: list[_FakeTextChannel]):
        self.id = channel_id
        self.name = name
        self.channels = channels
        self.deleted = False

    async def delete(self, *, reason: str):
        self.deleted = True


class _FakeGuild:
    def __init__(self, guild_id: int, channels):
        self.id = guild_id
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


@pytest.mark.asyncio
async def test_reset_deletes_only_tracked_matchup_channels_and_empty_categories(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(season_lifecycle.discord, "TextChannel", _FakeTextChannel)
    monkeypatch.setattr(season_lifecycle.discord, "CategoryChannel", _FakeCategoryChannel)

    db = Database(tmp_path / "discord-reset.sqlite3")
    await db.initialize()
    await db.update_settings(1, season="Demo", final_scores_channel_id=900)
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,status,channel_id,
            created_at,updated_at)
           VALUES (1,'Demo',1,'tracked','Bears','Packers','waiting',101,'now','now'),
                  (1,'Demo',1,'protected','Bills','Jets','waiting',900,'now','now'),
                  (1,'Demo',2,'tracked-empty','49ers','Rams','waiting',103,'now','now')"""
    )
    await db.execute(
        """INSERT INTO week_categories (guild_id,season,week,category_id,created_at)
           VALUES (1,'Demo',1,201,'now'),(1,'Demo',2,202,'now')"""
    )

    tracked = _FakeTextChannel(101, "bears-at-packers")
    manual = _FakeTextChannel(102, "hazrat-notes")
    empty_tracked = _FakeTextChannel(103, "49ers-at-rams")
    protected = _FakeTextChannel(900, "final-scores")
    mixed_category = _FakeCategoryChannel(201, "WEEK 1 MATCHUPS", [tracked, manual])
    empty_category = _FakeCategoryChannel(202, "WEEK 2 MATCHUPS", [empty_tracked])
    guild = _FakeGuild(
        1, [tracked, manual, empty_tracked, protected, mixed_category, empty_category]
    )

    warnings = await season_lifecycle.purge_active_season_discord_content(
        guild, db, season="Demo"
    )

    assert tracked.deleted is True
    assert empty_tracked.deleted is True
    assert manual.deleted is False
    assert protected.deleted is False
    assert mixed_category.deleted is False
    assert empty_category.deleted is True
    assert any("configured channel 900" in item for item in warnings)
    assert any("Preserved category WEEK 1 MATCHUPS" in item for item in warnings)

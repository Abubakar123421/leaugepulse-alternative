from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leaguebot.db import Database
from leaguebot import services


async def setup_reminder(tmp_path, monkeypatch, remaining=48, status='waiting', scheduled=False):
    now = datetime(2026, 10, 6, 10, 37, tzinfo=UTC)
    monkeypatch.setattr(services, 'utcnow', lambda: now)
    monkeypatch.setattr(services, 'iso_now', lambda: services.utcnow().isoformat())
    db = Database(tmp_path / 'reminders.sqlite3')
    await db.initialize()
    await db.update_settings(1, season='1')
    await db.execute(
        '''INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,
            away_user_id,home_user_id,channel_id,status,scheduled_at,
            deadline_at,created_at,updated_at)
           VALUES (1,'1',1,'test','Away','Home',10,20,30,?,?,?,?,?)''',
        (status, now.isoformat() if scheduled else None,
         (now + timedelta(hours=remaining)).isoformat(),
         now.isoformat(), now.isoformat()),
    )
    class Channel:
        send = AsyncMock()
    channel = Channel()
    monkeypatch.setattr(services.discord, 'TextChannel', Channel)
    bot = SimpleNamespace(get_channel=lambda _: channel, get_guild=lambda _: None)
    return db, bot, channel, now


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining', [48, 24, 6])
async def test_twelve_hour_cadence_and_restart(tmp_path, monkeypatch, remaining):
    db, bot, channel, now = await setup_reminder(tmp_path, monkeypatch, remaining)
    service = services.ReminderService(bot, db, 300)
    # Put the creation time 12 hours earlier without changing the advance deadline.
    await db.execute('UPDATE matchups SET created_at=?', ((now - timedelta(hours=12)).isoformat(),))
    await service.tick()
    assert channel.send.await_count == 1
    assert '<@10> <@20>' in channel.send.call_args.args[0]
    service = services.ReminderService(bot, db, 300)
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(hours=11, minutes=59))
    # Extend advance so the overdue reminder cannot mask the cadence check.
    await db.execute('UPDATE matchups SET deadline_at=?', ((now + timedelta(days=3)).isoformat(),))
    await service.tick()
    assert channel.send.await_count == 1
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(hours=12))
    await service.tick()
    await service.tick()
    assert channel.send.await_count == 2


@pytest.mark.asyncio
async def test_first_reminder_waits_twelve_hours(tmp_path, monkeypatch):
    db, bot, channel, now = await setup_reminder(tmp_path, monkeypatch)
    service = services.ReminderService(bot, db, 300)
    await service.tick()
    channel.send.assert_not_awaited()
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(hours=12))
    await service.tick()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,scheduled', [
    ('scheduled', False), ('waiting', True), ('result_pending', False),
    ('complete', False), ('force_home', False), ('force_away', False), ('fair_sim', False),
])
async def test_scheduled_or_submitted_matches_stop_even_overdue(tmp_path, monkeypatch, status, scheduled):
    db, bot, channel, _ = await setup_reminder(tmp_path, monkeypatch, -1, status, scheduled)
    await services.ReminderService(bot, db, 300).tick()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_overdue_sent_once_across_restarts(tmp_path, monkeypatch):
    db, bot, channel, now = await setup_reminder(tmp_path, monkeypatch, -1)
    await services.ReminderService(bot, db, 300).tick()
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(days=2))
    await services.ReminderService(bot, db, 300).tick()
    channel.send.assert_awaited_once()
    assert 'deadline has passed' in channel.send.call_args.args[0]

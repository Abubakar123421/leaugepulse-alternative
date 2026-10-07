from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leaguebot.db import Database
from leaguebot import services
from leaguebot.stream_accounts import twitch_login, youtube_lookup, register_accounts, resolve_youtube

CHANNEL = 'UC' + 'a' * 22

class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.data
    def raise_for_status(self):
        if self.status != 200:
            raise ValueError('HTTP failure')

class Session:
    def __init__(self, streams=None, items=None):
        self.streams, self.items = streams or [], items or []
        self.calls = []
        self.status = 200
    def post(self, url, **kwargs):
        return Response({'access_token': 'test-token'})
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/channels'):
            return Response({'items': [{'id': CHANNEL}]})
        if url.endswith('/streams'):
            return Response({'data': self.streams}, self.status)
        return Response({'items': self.items}, self.status)

async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'streams.sqlite3')
    await db.initialize()
    for guild in (1, 2):
        await db.update_settings(guild, season='1', streams_channel_id=guild * 100)
        await db.execute('''INSERT INTO profiles (guild_id,user_id,team_name,approved,twitch,youtube,updated_at)
                            VALUES (?,10,'Away',1,'player',?,'now')''', (guild, f'https://youtube.com/channel/{CHANNEL}'))
        await db.execute('''INSERT INTO matchups (guild_id,season,week,external_key,away_team,home_team,away_user_id,created_at,updated_at)
                            VALUES (?,'1',1,'game','Away','Home',10,'now','now')''', (guild,))
    class Channel:
        def __init__(self, guild):
            self.guild = SimpleNamespace(id=guild)
            self.send = AsyncMock(return_value=SimpleNamespace(id=guild * 1000))
    channels = {guild * 100: Channel(guild) for guild in (1, 2)}
    monkeypatch.setattr(services.discord, 'TextChannel', Channel)
    bot = SimpleNamespace(get_channel=lambda id: channels.get(id))
    service = services.StreamService(bot, db, 180, 'client', 'secret', 'key')
    return db, bot, service, channels

@pytest.mark.parametrize('value', ['player', 'https://www.twitch.tv/PLAYER', 'twitch.tv/player/'])
def test_twitch_normalization(value):
    assert twitch_login(value) == 'player'

@pytest.mark.parametrize('value', ['https://evil.test/player', 'https://twitch.tv/player/extra', 'bad name'])
def test_twitch_rejects_invalid_accounts(value):
    with pytest.raises(ValueError):
        twitch_login(value)

@pytest.mark.parametrize('value', [CHANNEL, f'https://youtube.com/channel/{CHANNEL}'])
def test_youtube_ids(value):
    assert youtube_lookup(value) == {'id': CHANNEL}

@pytest.mark.asyncio
async def test_youtube_handle_resolution():
    session = Session()
    assert await resolve_youtube(session, 'https://youtube.com/@Player', 'key') == CHANNEL
    assert session.calls[0][1]['params']['forHandle'] == '@Player'
    with pytest.raises(ValueError):
        await resolve_youtube(session, 'https://youtube.com/watch?v=abc', 'key')

@pytest.mark.asyncio
async def test_registration_preserves_other_account_and_isolates_guild(tmp_path, monkeypatch):
    db, _, _, _ = await setup(tmp_path, monkeypatch)
    await register_accounts(db, 1, 10, twitch='newplayer')
    row = await db.fetchone('SELECT * FROM profiles WHERE guild_id=1')
    assert row['youtube'].endswith(CHANNEL)
    assert row['twitch'] == 'newplayer'
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=2'))['twitch'] == 'player'
    with pytest.raises(ValueError):
        await register_accounts(db, 1, 999, twitch='anyone')
    await db.execute("INSERT INTO profiles (guild_id,user_id,team_name,approved,updated_at) VALUES (1,20,'Home',1,'now')")
    with pytest.raises(ValueError):
        await register_accounts(db, 1, 20, twitch='newplayer')

@pytest.mark.asyncio
async def test_twitch_filter_title_change_and_restart(tmp_path, monkeypatch):
    db, bot, service, channels = await setup(tmp_path, monkeypatch)
    session = Session(streams=[{'id': 'live1', 'user_login': 'player', 'type': 'live', 'title': 'COD tonight'}])
    await service._tick_twitch(session)
    assert not channels[100].send.called
    session.streams[0]['title'] = 'GRIDIRON LEGENDS — Week 1'
    await service._tick_twitch(session)
    embed = channels[100].send.call_args.kwargs['embed']
    assert embed.url == 'https://www.twitch.tv/player'
    assert any('Away @ Home' in field.value for field in embed.fields)
    assert any('<@10>' in field.value for field in embed.fields)
    assert channels[200].send.await_count == 1
    service = services.StreamService(bot, db, 180, 'client', 'secret', None)
    await service._tick_twitch(session)
    assert channels[100].send.await_count == 1
    session.streams[0]['id'] = 'live2'
    await service._tick_twitch(session)
    assert channels[100].send.await_count == 2

@pytest.mark.asyncio
async def test_youtube_live_registered_title_and_budget(tmp_path, monkeypatch):
    db, bot, service, channels = await setup(tmp_path, monkeypatch)
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    monkeypatch.setattr(services, 'utcnow', lambda: now)
    def video(id, channel, title, live='live'):
        return {'id': {'videoId': id}, 'snippet': {'channelId': channel, 'title': title, 'liveBroadcastContent': live}}
    session = Session(items=[video('other', 'UCunknown', 'Gridiron Legends'),
                             video('cod', CHANNEL, 'COD'),
                             video('soon', CHANNEL, 'Gridiron Legends', 'upcoming'),
                             video('yes', CHANNEL, 'gridiron legends &amp; playoffs')])
    await service._tick_youtube(session)
    assert channels[100].send.await_count == 1
    assert channels[100].send.call_args.kwargs['embed'].url.endswith('v=yes')
    await service._tick_youtube(session)
    assert len(session.calls) == 1
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(minutes=17))
    restarted = services.StreamService(bot, db, 180, None, None, 'key')
    await restarted._tick_youtube(session)
    assert len(session.calls) == 2
    assert channels[100].send.await_count == 1
    await db.execute('UPDATE youtube_search_usage SET requests=90')
    monkeypatch.setattr(services, 'utcnow', lambda: now + timedelta(hours=1))
    await restarted._tick_youtube(session)
    assert len(session.calls) == 2

@pytest.mark.asyncio
async def test_disabled_streams_and_missing_destination(tmp_path, monkeypatch):
    db, _, service, channels = await setup(tmp_path, monkeypatch)
    await db.update_settings(1, features='{"streams":false}')
    await db.update_settings(2, streams_channel_id=None)
    session = Session(streams=[{'id': 'live', 'user_login': 'player', 'type': 'live', 'title': 'Gridiron Legends'}])
    await service._tick_twitch(session)
    assert not session.calls
    assert not channels[100].send.called

@pytest.mark.asyncio
async def test_shutdown_without_task(tmp_path, monkeypatch):
    _, _, service, _ = await setup(tmp_path, monkeypatch)
    await service.close()

@pytest.mark.asyncio
async def test_registration_commands_require_commissioner(tmp_path, monkeypatch):
    from leaguebot.bot import LeagueBot, register_commands
    from leaguebot.config import Config
    bot = LeagueBot(Config.from_env(require_token=False))
    bot.db = Database(tmp_path / 'commands.sqlite3')
    await bot.db.initialize()
    register_commands(bot)
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=1, guild=SimpleNamespace(id=1), user=SimpleNamespace(id=9), response=response)
    member = SimpleNamespace(id=10)
    await bot.tree.get_command('registerstreams').callback(interaction, member, twitch='player')
    response.send_message.assert_awaited_once_with('Only a configured commissioner can do that.', ephemeral=True)
    assert not await bot.db.fetchall('SELECT * FROM profiles')
    await bot.close()

@pytest.mark.asyncio
async def test_legacy_session_is_not_reannounced(tmp_path, monkeypatch):
    db, _, service, channels = await setup(tmp_path, monkeypatch)
    await db.execute("INSERT INTO stream_alert_state (guild_id,platform,channel_key,live_id) VALUES (1,'twitch','player','old')")
    session = Session(streams=[{'id': 'old', 'user_login': 'player', 'type': 'live', 'title': 'Gridiron Legends'}])
    await service._tick_twitch(session)
    channels[100].send.assert_not_awaited()
    channels[200].send.assert_awaited_once()

@pytest.mark.asyncio
async def test_twitch_auth_expiry_and_youtube_failure_are_independent(tmp_path, monkeypatch):
    _, _, service, channels = await setup(tmp_path, monkeypatch)
    service._twitch_token = 'expired'
    session = Session()
    session.status = 401
    await service._tick_twitch(session)
    assert service._twitch_token is None
    session.status = 200
    service._tick_youtube = AsyncMock(side_effect=ValueError('API failure'))
    service._tick_twitch = AsyncMock()
    await service.tick(session)
    service._tick_twitch.assert_awaited_once()

@pytest.mark.asyncio
async def test_ambiguous_delivery_does_not_repeat(tmp_path, monkeypatch):
    import aiohttp
    _, _, service, channels = await setup(tmp_path, monkeypatch)
    profiles = await service._profiles('twitch')
    profile = next(p for p in profiles if p['guild_id'] == 1)
    channels[100].send.side_effect = aiohttp.ClientConnectionError('connection lost')
    with pytest.raises(aiohttp.ClientConnectionError):
        await service._announce(profile, 'twitch', 'player', 'one', 'Gridiron Legends', 'https://twitch.tv/player')
    channels[100].send.side_effect = None
    await service._announce(profile, 'twitch', 'player', 'one', 'Gridiron Legends', 'https://twitch.tv/player')
    assert channels[100].send.await_count == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('title,announced', [
    (' gridiron legends stmh ', True),
    ('GRIDIRON LEGENDS | Week 2', True),
    ('Madden — Gridiron Legends playoffs', True),
    ('Other Madden League', False),
    ('COD tonight', False),
    ('Gridiron unrelated Legends', False),
])
async def test_exact_title_phrase_with_extra_text(tmp_path, monkeypatch, title, announced):
    _, _, service, channels = await setup(tmp_path, monkeypatch)
    profile = next(p for p in await service._profiles('youtube') if p['guild_id'] == 1)
    await service._announce(profile, 'youtube', CHANNEL, 'title-test', title,
                            'https://www.youtube.com/watch?v=title-test')
    assert channels[100].send.await_count == int(announced)

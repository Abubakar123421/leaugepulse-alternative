import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from leaguebot.db import Database
from leaguebot.helpers import iso_now
from leaguebot.web_api import WebsiteAPI, display_status, wire
from leaguebot.web_bridge import WebBridge, WebInteraction


@pytest_asyncio.fixture
async def site(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_API_SECRET', 's' * 40)
    monkeypatch.setenv('WEB_GUILD_IDS', '1,2')
    monkeypatch.setenv('WEB_ORIGIN', 'https://league.example')
    db = Database(tmp_path / 'league.sqlite3')
    await db.initialize()
    for gid in (1, 2):
        await db.update_settings(gid, season='1')
        await db.execute("INSERT INTO teams (guild_id,season,name) VALUES (?,'1','Away'),(?,'1','Home')", (gid, gid))
    members = {10: SimpleNamespace(id=10, display_name='Owner', bot=False),
               99: SimpleNamespace(id=99, display_name='Admin', bot=False)}
    guilds = {gid: SimpleNamespace(id=gid, name=f'League {gid}', icon=None, text_channels=[], roles=[],
        members=list(members.values()), fetch_member=AsyncMock(side_effect=lambda uid: members[uid]),
        get_channel=lambda _: None, get_member=lambda uid: members.get(uid)) for gid in (1, 2)}
    bot = SimpleNamespace(db=db, config=SimpleNamespace(stream_poll_seconds=180),
        get_guild=lambda gid: guilds.get(gid), is_ready=lambda: True,
        tree=SimpleNamespace(get_commands=lambda: []))
    monkeypatch.setattr('leaguebot.web_api.is_commissioner', AsyncMock(side_effect=lambda i, s: i.user.id == 99))
    api = WebsiteAPI(bot)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    yield api, client, db, guilds
    await client.close()


async def call(site, path, actor=10, data=None, key=None, nonce=None, stamp=None):
    api, client, *_ = site
    method = 'POST' if data is not None else 'GET'
    body = json.dumps(data) if data is not None else ''
    stamp, nonce = str(stamp or int(time.time())), nonce or uuid.uuid4().hex
    canonical = '\n'.join([method, path, stamp, nonce, str(actor), hashlib.sha256(body.encode()).hexdigest()])
    headers = {'X-Web-Time': stamp, 'X-Web-Nonce': nonce, 'X-Web-Actor': str(actor),
        'X-Web-Signature': hmac.new(api.secret.encode(), canonical.encode(), hashlib.sha256).hexdigest(),
        'X-Request-Key': key or uuid.uuid4().hex, 'Content-Type': 'application/json'}
    return await client.request(method, path, data=body if data is not None else None, headers=headers)


@pytest.mark.asyncio
async def test_signature_replay_expiration_and_membership(site):
    api, client, db, guilds = site
    assert (await client.get('/v1/servers')).status == 401
    nonce = uuid.uuid4().hex
    assert (await call(site, '/v1/servers', nonce=nonce)).status == 200
    assert (await call(site, '/v1/servers', nonce=nonce)).status == 401
    assert (await call(site, '/v1/servers', stamp=int(time.time()) - 120)).status == 401
    assert (await call(site, '/v1/servers/3/snapshot')).status == 404
    assert (await call(site, '/v1/servers/1/snapshot', actor=0)).status == 403
    await db.execute('INSERT INTO web_leagues VALUES (1,1)')
    assert (await call(site, '/v1/servers/1/snapshot', actor=0)).status == 200
    assert (await call(site, '/v1/servers/1/visibility', actor=0, data={'public': False})).status == 403
    guilds[1].fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'Missing member')
    assert (await call(site, '/v1/servers/1/snapshot')).status == 403


@pytest.mark.asyncio
async def test_record_write_is_scoped_audited_and_idempotent(site):
    _, _, db, _ = site
    data = {'season': '1', 'team': 'Away', 'wins': 2, 'losses': 1, 'ties': 0, 'reason': 'Correct imported historical totals'}
    assert (await call(site, '/v1/servers/1/record-adjustment', data=data)).status == 403
    key = uuid.uuid4().hex
    assert (await call(site, '/v1/servers/1/record-adjustment', actor=99, data=data, key=key)).status == 200
    assert (await call(site, '/v1/servers/1/record-adjustment', actor=99, data=data, key=key)).status == 200
    assert (await call(site, '/v1/servers/1/record-adjustment', actor=99, data={**data, 'wins': 3}, key=key)).status == 409
    assert (await db.fetchone("SELECT wins FROM teams WHERE guild_id=1 AND name='Away'"))['wins'] == 2
    assert (await db.fetchone("SELECT wins FROM teams WHERE guild_id=2 AND name='Away'"))['wins'] == 0
    assert len(await db.fetchall('SELECT * FROM web_record_adjustments')) == 1
    assert len(await db.fetchall('SELECT * FROM web_outbox')) == 1
    assert (await call(site, '/v1/servers/1/command', actor=99, data={'name': 'backup', 'values': {}})).status == 403


@pytest.mark.asyncio
async def test_discord_database_updates_produce_private_and_public_notifications(site):
    _, _, db, _ = site
    now = iso_now()
    mid = await db.execute("INSERT INTO matchups (guild_id,season,week,external_key,away_team,home_team,away_user_id,home_user_id,created_at,updated_at) VALUES (1,'1',1,'test','Away','Home',10,20,?,?)", (now, now))
    await db.execute("UPDATE matchups SET status='result_pending' WHERE id=?", (mid,))
    await db.audit(1, 99, 'private_secret', details={'secret': 'must-not-leak'})
    await db.execute('INSERT INTO web_leagues VALUES (1,1)')
    public = await (await call(site, '/v1/servers/1/snapshot', actor=0)).json()
    assert public['events'] == [] and 'audit' not in public and 'deliveries' not in public
    owner = await (await call(site, '/v1/servers/1/snapshot')).json()
    assert len(owner['events']) == 1 and owner['unread'] == 1
    assert (await call(site, '/v1/servers/1/read-notifications', data={})).status == 200
    assert (await (await call(site, '/v1/servers/1/snapshot')).json())['unread'] == 0
    await db.execute("UPDATE matchups SET status='scheduled' WHERE id=?", (mid,))
    assert len((await (await call(site, '/v1/servers/1/snapshot', actor=0)).json())['events']) == 1
    assert (await (await call(site, '/v1/servers/2/snapshot')).json())['events'] == []
    assert (await call(site, '/v1/servers/2/matchup', data={'id': mid, 'action': 'manage'})).status == 403
    await db.initialize()  # migration restart preserves existing game data
    assert (await db.fetchone('SELECT status FROM matchups WHERE id=?', (mid,)))['status'] == 'scheduled'


@pytest.mark.asyncio
async def test_override_finalizes_records_and_delivery_once(site):
    _, _, db, _ = site
    now = iso_now()
    mid = await db.execute("INSERT INTO matchups (guild_id,season,week,external_key,away_team,home_team,created_at,updated_at) VALUES (1,'1',1,'test','Away','Home',?,?)", (now, now))
    data = {'id': mid, 'action': 'override', 'awayScore': 21, 'homeScore': 7, 'reason': 'Verified by commissioner after disconnect'}
    assert (await call(site, '/v1/servers/1/matchup', actor=99, data=data)).status == 200
    assert (await call(site, '/v1/servers/1/matchup', actor=99, data=data)).status == 400
    row = await db.fetchone('SELECT * FROM matchups WHERE id=?', (mid,))
    assert row['status'] == 'complete' and 'disconnect' in row['result_review_note']
    assert (await db.fetchone("SELECT wins FROM teams WHERE guild_id=1 AND name='Away'"))['wins'] == 1
    assert len(await db.fetchall("SELECT * FROM web_outbox WHERE kind='result_publish'")) == 1
    assert len(await db.fetchall("SELECT * FROM audit_logs WHERE action='result_override'")) == 1


@pytest.mark.asyncio
async def test_bridge_controls_bound_to_actor_server_and_current_role():
    bridge = WebBridge(None)
    view = discord.ui.View()
    button = discord.ui.Button(label='Admin action')
    button.callback = AsyncMock()
    view.add_item(button)
    i = WebInteraction(None, SimpleNamespace(id=1), SimpleNamespace(id=99))
    i.web_admin, i.view = True, view
    token = bridge.serialize(i)['controls'][0]['token']
    with pytest.raises(ValueError):
        await bridge.respond(token, WebInteraction(None, SimpleNamespace(id=2), SimpleNamespace(id=99)))
    i.web_admin = False
    with pytest.raises(ValueError, match='removed'):
        await bridge.respond(token, i)
    button.callback.assert_not_called()
    i.web_admin = True
    token = bridge.serialize(i)['controls'][0]['token']
    await bridge.respond(token, i)
    button.callback.assert_awaited_once()
    with pytest.raises(ValueError):
        await bridge.respond(token, i)


def test_five_server_limit_snowflakes_and_states(monkeypatch):
    monkeypatch.setenv('WEB_GUILD_IDS', '1,2,3,4,5,6')
    with pytest.raises(ValueError, match='five'):
        WebsiteAPI(SimpleNamespace(db=None))
    assert wire({'user_id': 123456789123456789})['user_id'] == '123456789123456789'
    assert display_status({'status': 'complete', 'deadline_at': '2020-01-01T00:00:00+00:00'}) == 'Completed'
    assert display_status({'status': 'waiting', 'deadline_at': '2020-01-01T00:00:00+00:00'}) == 'Overdue'
    assert display_status({'status': 'scheduled'}) == 'Scheduled'
    assert display_status({'status': 'waiting'}) == 'Not Scheduled'


@pytest.mark.asyncio
async def test_command_bridge_keeps_choices_defaults_and_ranges():
    captured = []
    @discord.app_commands.command(name='test-command', description='Bridge test')
    @discord.app_commands.choices(mode=[discord.app_commands.Choice(name='One', value='one')])
    async def command(interaction, mode: discord.app_commands.Choice[str], week: discord.app_commands.Range[int, 1, 4], optional: str = 'default'):
        captured.append((mode.value, week, optional))
        await interaction.response.send_message('Saved')
    bot = SimpleNamespace(tree=SimpleNamespace(get_command=lambda _: command))
    bridge = WebBridge(bot)
    i = WebInteraction(bot, SimpleNamespace(id=1), SimpleNamespace(id=10))
    result = await bridge.command('test-command', {'mode': 'one', 'week': '2'}, i)
    assert captured == [('one', 2, 'default')] and result['messages'][0]['content'] == 'Saved'
    with pytest.raises(ValueError):
        await bridge.command('test-command', {'mode': 'one', 'week': 9}, i)


def test_all_existing_commands_have_website_schema():
    from leaguebot.bot import LeagueBot, register_commands
    from leaguebot.config import Config
    bot = LeagueBot(Config.from_env(require_token=False))
    register_commands(bot)
    schemas = [WebBridge(bot).command_schema(c) for c in bot.tree.get_commands()]
    assert len(schemas) == 58
    assert {'backup', 'importfixtures', 'season-force-delete', 'requeststream'} <= {s['name'] for s in schemas}


@pytest.mark.asyncio
async def test_discord_backup_matches_operator_policy_when_web_enabled(site, monkeypatch):
    from leaguebot.bot import LeagueBot, register_commands
    from leaguebot.config import Config
    api, _, db, guilds = site
    bot = LeagueBot(Config.from_env(require_token=False))
    bot.db, bot.website = db, api
    register_commands(bot)
    monkeypatch.setattr('leaguebot.bot.require_commissioner', AsyncMock(return_value=True))
    interaction = WebInteraction(bot, guilds[1], await guilds[1].fetch_member(99))
    await bot.tree.get_command('backup').callback(interaction)
    assert 'platform operator' in interaction.messages[0]['content']
    assert not await db.fetchall("SELECT * FROM audit_logs WHERE action='backup'")


@pytest.mark.asyncio
async def test_manual_server_registration_is_authorized_persistent_private_and_capped(site, monkeypatch):
    from leaguebot.checks import is_commissioner
    from leaguebot.web_access import APPOINTED_COMMISSIONERS
    api, _, db, guilds = site
    members = {uid: Mock(spec=discord.Member, id=uid, bot=False, display_name=str(uid),
        guild_permissions=SimpleNamespace(administrator=False), get_role=lambda _: None) for uid in APPOINTED_COMMISSIONERS}
    for gid in range(1, 7):
        guilds[gid] = SimpleNamespace(id=gid, name=f'League {gid}', icon=None, roles=[], text_channels=[],
            members=list(members.values()), get_channel=lambda _: None, get_member=lambda uid: members.get(uid),
            fetch_member=AsyncMock(side_effect=lambda uid: members[uid]))
    api.bot.website = api
    monkeypatch.setattr('leaguebot.web_api.is_commissioner', is_commissioner)
    actor = 741159046819741708
    assert (await call(site, '/v1/servers', actor=10, data={'guildId': '3'})).status == 403
    assert (await call(site, '/v1/servers', actor=0, data={'guildId': '3'})).status == 403
    assert (await call(site, '/v1/servers', actor=actor, data={'guildId': 'garbage'})).status == 400
    for gid in (3, 4, 5):
        response = await call(site, '/v1/servers', actor=actor, data={'guildId': str(gid)})
        assert response.status == 200
        data = await response.json()
        assert data['canRegisterServer'] and data['enabledCount'] == gid
        assert all(s['role'] == 'commissioner' for s in data['servers'])
    assert (await call(site, '/v1/servers', actor=actor, data={'guildId': '6'})).status == 409
    assert (await call(site, '/v1/servers', actor=actor, data={'guildId': '5'})).status == 200
    assert len(await db.fetchall('SELECT * FROM web_server_registry')) == 3
    assert len(await db.fetchall("SELECT * FROM audit_logs WHERE action='website_server_registered'")) == 3
    assert (await call(site, '/v1/servers/3/snapshot', actor=0)).status == 403
    assert (await (await call(site, '/v1/servers/3/snapshot', actor=1014858487970414663)).json())['role'] == 'commissioner'
    restarted = WebsiteAPI(api.bot)
    assert await restarted.refresh_guild_ids() == {1, 2, 3, 4, 5}
    guilds[3].fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'Missing member')
    assert (await call(site, '/v1/servers/3/snapshot', actor=actor)).status == 403


@pytest.mark.asyncio
async def test_registration_requires_shared_membership_and_does_not_touch_unrelated_servers(site):
    api, _, db, guilds = site
    actor = 741159046819741708
    assert (await call(site, '/v1/servers', actor=actor, data={'guildId': '99'})).status == 400
    guilds[1].fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'Missing member')
    assert (await call(site, '/v1/servers', actor=actor, data={'guildId': '1'})).status == 403
    assert not await db.fetchall('SELECT * FROM web_server_registry')

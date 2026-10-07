import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from leaguebot.db import Database
from leaguebot.stream_accounts import (
    request_account, decide_account_request, remove_account, register_accounts,
)
from leaguebot import stream_ui
from leaguebot.bot import LeagueBot, register_commands
from leaguebot.config import Config

CHANNEL = 'UC' + 'b' * 22

@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / 'requests.sqlite3')
    await db.initialize()
    await db.initialize()
    for guild in (1, 2):
        await db.update_settings(guild, season='1', audit_channel_id=100 * guild)
        await db.execute("INSERT INTO profiles (guild_id,user_id,team_name,approved,updated_at) VALUES (?,10,'Away',1,'now')", (guild,))
    return db

@pytest.mark.asyncio
@pytest.mark.parametrize('platform,account', [('twitch','newplayer'), ('youtube',CHANNEL)])
async def test_request_stays_inactive_until_approval_and_survives_restart(db, platform, account):
    id = await request_account(db, 1, 10, platform, account)
    assert (await db.fetchone('SELECT * FROM profiles WHERE guild_id=1'))[platform] is None
    restarted = Database(db.path)
    await restarted.initialize()
    await decide_account_request(restarted, 1, id, approve=True, actor_id=99)
    row = await db.fetchone('SELECT * FROM profiles WHERE guild_id=1')
    assert row[platform] == (account if platform == 'twitch' else f'https://www.youtube.com/channel/{account}')
    assert (await db.fetchone('SELECT * FROM profiles WHERE guild_id=2'))[platform] is None
    with pytest.raises(ValueError):
        await decide_account_request(db, 1, id, approve=True, actor_id=99)

@pytest.mark.asyncio
async def test_nonmembers_and_unapproved_owners_cannot_request(db):
    with pytest.raises(ValueError):
        await request_account(db, 1, 999, 'twitch', 'newplayer')
    await db.execute('UPDATE profiles SET approved=0 WHERE guild_id=1')
    with pytest.raises(ValueError):
        await request_account(db, 1, 10, 'twitch', 'newplayer')

@pytest.mark.asyncio
async def test_reject_preserves_existing_account(db):
    await register_accounts(db, 1, 10, twitch='original')
    id = await request_account(db, 1, 10, 'twitch', 'replacement')
    await decide_account_request(db, 1, id, approve=False, actor_id=99)
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] == 'original'
    assert (await db.fetchone('SELECT status FROM stream_account_requests WHERE id=?', (id,)))['status'] == 'rejected'

@pytest.mark.asyncio
async def test_pending_request_and_cross_guild_protection(db):
    id = await request_account(db, 1, 10, 'twitch', 'newplayer')
    with pytest.raises(ValueError):
        await request_account(db, 1, 10, 'twitch', 'replacement')
    with pytest.raises(ValueError):
        await decide_account_request(db, 2, id, approve=True, actor_id=99)
    assert (await db.fetchone('SELECT * FROM profiles WHERE guild_id=1'))['twitch'] is None

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ["UPDATE profiles SET approved=0 WHERE guild_id=1",
                                     "UPDATE profiles SET team_name='New Team' WHERE guild_id=1",
                                     "UPDATE guild_settings SET season='2' WHERE guild_id=1"])
async def test_approval_rechecks_team_and_season(db, change):
    id = await request_account(db, 1, 10, 'twitch', 'newplayer')
    await db.execute(change)
    with pytest.raises(ValueError):
        await decide_account_request(db, 1, id, approve=True, actor_id=99)
    await decide_account_request(db, 1, id, approve=False, actor_id=99)

@pytest.mark.asyncio
@pytest.mark.parametrize('actor', [10, 99])
async def test_self_or_commissioner_removal_cancels_pending_and_keeps_other_platform(db, actor):
    await register_accounts(db, 1, 10, twitch='original', youtube=CHANNEL)
    id = await request_account(db, 1, 10, 'twitch', 'replacement')
    await remove_account(db, 1, 10, 'twitch', actor_id=actor)
    row = await db.fetchone('SELECT * FROM profiles WHERE guild_id=1')
    assert row['twitch'] is None and row['youtube'].endswith(CHANNEL)
    assert (await db.fetchone('SELECT status FROM stream_account_requests WHERE id=?', (id,)))['status'] == 'cancelled'
    with pytest.raises(ValueError):
        await decide_account_request(db, 1, id, approve=True, actor_id=99)

@pytest.mark.asyncio
async def test_duplicate_accounts_rechecked_at_approval(db):
    await db.execute("INSERT INTO profiles (guild_id,user_id,team_name,approved,updated_at) VALUES (1,20,'Home',1,'now')")
    id = await request_account(db, 1, 10, 'twitch', 'newplayer')
    await register_accounts(db, 1, 20, twitch='newplayer')
    with pytest.raises(ValueError):
        await decide_account_request(db, 1, id, approve=True, actor_id=99)
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1 AND user_id=10'))['twitch'] is None

@pytest.mark.asyncio
async def test_direct_commissioner_update_supersedes_old_request(db):
    id = await request_account(db, 1, 10, 'twitch', 'newplayer')
    await register_accounts(db, 1, 10, twitch='commissionerchoice')
    with pytest.raises(ValueError):
        await decide_account_request(db, 1, id, approve=True, actor_id=99)

@pytest.mark.asyncio
async def test_concurrent_decisions_resolve_once(db):
    id = await request_account(db, 1, 10, 'twitch', 'newplayer')
    results = await asyncio.gather(
        decide_account_request(db, 1, id, approve=True, actor_id=99),
        decide_account_request(db, 1, id, approve=False, actor_id=100), return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert len(await db.fetchall("SELECT * FROM audit_logs WHERE action LIKE 'stream_request_%'")) == 1

class Audit:
    id = 100
    guild = SimpleNamespace(id=1)
    def __init__(self):
        self.send = AsyncMock(return_value=SimpleNamespace(id=500))


def interaction(bot, *, user_id=10):
    return SimpleNamespace(
        guild_id=1, guild=SimpleNamespace(id=1, name='League', get_member=lambda id: SimpleNamespace(send=AsyncMock())),
        channel_id=100, client=bot, user=SimpleNamespace(id=user_id, mention=f'<@{user_id}>'),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(id=500, edit=AsyncMock()),
    )

@pytest.mark.asyncio
async def test_request_command_audit_buttons_and_self_removal(db, monkeypatch):
    bot = LeagueBot(Config.from_env(require_token=False))
    bot.db = db
    audit = Audit()
    register_commands(bot)
    monkeypatch.setattr(stream_ui.discord, 'TextChannel', Audit)
    monkeypatch.setattr(bot, 'get_channel', lambda id: audit if id == 100 else None)
    i = interaction(bot)
    await bot.tree.get_command('requeststream').callback(i, discord.app_commands.Choice(name='Twitch', value='twitch'), 'player')
    kwargs = audit.send.call_args.kwargs
    assert kwargs['view'].timeout is None
    assert [child.item.label for child in kwargs['view'].children] == ['Approve', 'Reject']
    request = await db.fetchone('SELECT * FROM stream_account_requests')
    assert request['audit_message_id'] == 500
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] is None
    monkeypatch.setattr(stream_ui, 'require_commissioner', AsyncMock(return_value=True))
    button = stream_ui.StreamRequestReviewButton(1, request['id'], 'approve')
    await button.callback(interaction(bot, user_id=99))
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] == 'player'
    await bot.tree.get_command('removemystream').callback(interaction(bot), discord.app_commands.Choice(name='Twitch', value='twitch'))
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] is None
    await bot.close()

@pytest.mark.asyncio
async def test_review_permissions_and_original_message(db, monkeypatch):
    id = await request_account(db, 1, 10, 'twitch', 'player')
    bot = SimpleNamespace(db=db)
    button = stream_ui.StreamRequestReviewButton(1, id, 'approve')
    denied = interaction(bot)
    await button.callback(denied)
    denied.response.send_message.assert_awaited_once()
    monkeypatch.setattr(stream_ui, 'require_commissioner', AsyncMock(return_value=True))
    forged = interaction(bot, user_id=99)
    await button.callback(forged)
    assert 'original' in forged.followup.send.call_args.args[0]
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] is None

@pytest.mark.asyncio
async def test_legacy_register_cannot_bypass_stream_review(db):
    bot = LeagueBot(Config.from_env(require_token=False))
    bot.db = db
    register_commands(bot)
    i = interaction(bot)
    await bot.tree.get_command('register').callback(i, 'Away', twitch='bypass')
    assert 'separate commissioner approval' in i.response.send_message.call_args.args[0]
    assert (await db.fetchone('SELECT twitch FROM profiles WHERE guild_id=1'))['twitch'] is None
    await bot.close()

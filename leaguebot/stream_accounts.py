"""Canonical account identifiers for commissioner stream registration."""
import re
from urllib.parse import unquote, urlsplit


def twitch_login(value: str) -> str:
    value = value.strip()
    if '://' in value or value.lower().startswith(('twitch.tv/', 'www.twitch.tv/')):
        url = urlsplit(value if '://' in value else 'https://' + value)
        if url.hostname not in {'twitch.tv', 'www.twitch.tv'}:
            raise ValueError('Use a Twitch username or twitch.tv channel link.')
        value = url.path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,25}', value):
        raise ValueError('Use a Twitch username or twitch.tv channel link.')
    return value.lower()


def youtube_lookup(value: str) -> dict[str, str]:
    value = value.strip()
    if '://' in value or value.lower().startswith(('youtube.com/', 'www.youtube.com/')):
        url = urlsplit(value if '://' in value else 'https://' + value)
        if url.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
            raise ValueError('Use a YouTube @handle, channel ID, or channel link.')
        value = unquote(url.path).strip('/')
    if value.startswith('channel/'):
        value = value.split('/')[1]
    if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
        return {'id': value}
    if value.startswith('@'):
        handle = value.split('/')[0]
        if len(handle) > 1 and not any(c.isspace() for c in handle):
            return {'forHandle': handle}
    if value.startswith('user/') and len(value.split('/')) >= 2:
        return {'forUsername': value.split('/')[1]}
    raise ValueError('Use a YouTube @handle, UC channel ID, or /channel/ link; video links are not accounts.')


async def resolve_youtube(session, value: str, api_key: str | None) -> str:
    lookup = youtube_lookup(value)
    if 'id' in lookup:
        return lookup['id']
    if not api_key:
        raise ValueError('Configure YOUTUBE_API_KEY first, or provide the YouTube UC channel ID.')
    async with session.get(
        'https://www.googleapis.com/youtube/v3/channels',
        params={'part': 'id', **lookup, 'key': api_key},
    ) as response:
        if response.status != 200:
            raise ValueError('YouTube could not verify this account. Check the API key and account link.')
        items = (await response.json()).get('items', [])
    if not items:
        raise ValueError('YouTube channel not found. Check the handle or channel link.')
    return items[0]['id']


async def _register_accounts(conn, guild_id, user_id, *, twitch=None, youtube=None):
    """Register approved accounts inside the caller's transaction."""
    cursor = await conn.execute(
        'SELECT * FROM profiles WHERE guild_id=? AND user_id=? AND approved=1',
        (guild_id, user_id),
    )
    profile = await cursor.fetchone()
    if not profile:
        raise ValueError('Assign this member a team first with /assign-team.')
    cursor = await conn.execute(
        'SELECT * FROM profiles WHERE guild_id=? AND user_id!=?', (guild_id, user_id),
    )
    for other in await cursor.fetchall():
        if twitch and other['twitch']:
            try:
                same = twitch_login(other['twitch']) == twitch
            except ValueError:
                same = False
            if same:
                raise ValueError('That Twitch account is already registered to another member.')
        if youtube and other['youtube']:
            try:
                same = youtube_lookup(other['youtube']).get('id') == youtube
            except ValueError:
                same = False
            if same:
                raise ValueError('That YouTube account is already registered to another member.')
    from .helpers import iso_now
    await conn.execute(
        'UPDATE profiles SET twitch=?,youtube=?,updated_at=? WHERE guild_id=? AND user_id=?',
        (twitch if twitch is not None else profile['twitch'],
         f'https://www.youtube.com/channel/{youtube}' if youtube is not None else profile['youtube'],
         iso_now(), guild_id, user_id),
    )

async def register_accounts(db, guild_id, user_id, *, twitch=None, youtube=None):
    if twitch is None and youtube is None:
        raise ValueError('Provide a Twitch account and/or a YouTube account.')
    async with db.connect() as conn:
        await conn.execute('BEGIN IMMEDIATE')
        await _register_accounts(conn, guild_id, user_id, twitch=twitch, youtube=youtube)
        from .helpers import iso_now
        for platform, value in [('twitch', twitch), ('youtube', youtube)]:
            if value is not None:
                await conn.execute(
                    "UPDATE stream_account_requests SET status='superseded',updated_at=? WHERE guild_id=? AND user_id=? AND platform=? AND status='pending'",
                    (iso_now(), guild_id, user_id, platform),
                )
        await conn.commit()


def validate_platform(platform):
    if platform not in {'twitch', 'youtube'}:
        raise ValueError('Choose Twitch or YouTube.')


async def request_account(db, guild_id, user_id, platform, account):
    from .helpers import iso_now
    import sqlite3
    validate_platform(platform)
    account = twitch_login(account) if platform == 'twitch' else youtube_lookup(account).get('id')
    if not account:
        raise ValueError('Resolve the YouTube channel before requesting it.')
    async with db.connect() as conn:
        await conn.execute('BEGIN IMMEDIATE')
        cursor = await conn.execute('SELECT season FROM guild_settings WHERE guild_id=?', (guild_id,))
        settings = await cursor.fetchone()
        cursor = await conn.execute('SELECT * FROM profiles WHERE guild_id=? AND user_id=? AND approved=1', (guild_id, user_id))
        profile = await cursor.fetchone()
        if not profile or not settings:
            raise ValueError('Only members with an approved team can request streaming accounts.')
        cursor = await conn.execute('SELECT twitch,youtube FROM profiles WHERE guild_id=? AND user_id!=?', (guild_id, user_id))
        for row in await cursor.fetchall():
            try:
                other = twitch_login(row['twitch']) if platform == 'twitch' and row['twitch'] else youtube_lookup(row['youtube']).get('id') if platform == 'youtube' and row['youtube'] else None
            except ValueError:
                other = None
            if account == other:
                raise ValueError('That account is already registered to another member.')
        await conn.execute("UPDATE stream_account_requests SET status='cancelled',updated_at=? WHERE guild_id=? AND user_id=? AND status='pending' AND (season!=? OR team_name!=?)", (iso_now(), guild_id, user_id, settings['season'], profile['team_name']))
        try:
            cursor = await conn.execute(
                """INSERT INTO stream_account_requests
                   (guild_id,season,user_id,team_name,platform,account,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (guild_id, settings['season'], user_id, profile['team_name'], platform, account, iso_now(), iso_now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError('You already have a pending request for this platform. Use /removemystream to cancel it first.') from None
        await conn.commit()
        return cursor.lastrowid


async def decide_account_request(db, guild_id, request_id, *, approve, actor_id):
    from .helpers import iso_now
    async with db.connect() as conn:
        await conn.execute('BEGIN IMMEDIATE')
        cursor = await conn.execute("SELECT * FROM stream_account_requests WHERE id=? AND guild_id=? AND status='pending'", (request_id, guild_id))
        request = await cursor.fetchone()
        if not request:
            raise ValueError('This request is no longer pending or belongs to another server.')
        if approve:
            cursor = await conn.execute("""SELECT p.* FROM profiles p JOIN guild_settings g ON g.guild_id=p.guild_id
                WHERE p.guild_id=? AND p.user_id=? AND p.approved=1 AND p.team_name=? AND g.season=?""",
                (guild_id, request['user_id'], request['team_name'], request['season']))
            if not await cursor.fetchone():
                raise ValueError('This member no longer owns the requesting team in the current season. Reject this request.')
            await _register_accounts(conn, guild_id, request['user_id'], **{request['platform']: request['account']})
        status = 'approved' if approve else 'rejected'
        await conn.execute('UPDATE stream_account_requests SET status=?,decided_by=?,updated_at=? WHERE id=? AND guild_id=?', (status, actor_id, iso_now(), request_id, guild_id))
        await conn.commit()
    await db.audit(guild_id, actor_id, f'stream_request_{status}', target_type='member', target_id=str(request['user_id']), details={'request_id': request_id, 'platform': request['platform']})
    return dict(request) | {'status': status}


async def remove_account(db, guild_id, user_id, platform, *, actor_id):
    from .helpers import iso_now
    validate_platform(platform)
    async with db.connect() as conn:
        await conn.execute('BEGIN IMMEDIATE')
        await conn.execute(f'UPDATE profiles SET {platform}=NULL,updated_at=? WHERE guild_id=? AND user_id=?', (iso_now(), guild_id, user_id))
        await conn.execute("UPDATE stream_account_requests SET status='cancelled',decided_by=?,updated_at=? WHERE guild_id=? AND user_id=? AND platform=? AND status='pending'", (actor_id, iso_now(), guild_id, user_id, platform))
        await conn.commit()
    await db.audit(guild_id, actor_id, 'stream_account_removed', target_type='member', target_id=str(user_id), details={'platform': platform})

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


async def register_accounts(db, guild_id, user_id, *, twitch=None, youtube=None):
    """Update only supplied accounts; reject account sharing within one guild."""
    if twitch is None and youtube is None:
        raise ValueError('Provide a Twitch account and/or a YouTube account.')
    async with db.connect() as conn:
        await conn.execute('BEGIN IMMEDIATE')
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
        await conn.commit()

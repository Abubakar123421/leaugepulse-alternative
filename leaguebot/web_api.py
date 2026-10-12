"""Optional same-process HTTP API for the Vercel website.

All routes except health and one-use uploads require timestamped HMAC signatures.
Only the Vercel server holds the signing secret; browsers never receive it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, UTC

import discord
from aiohttp import web

from .checks import is_commissioner
from .helpers import FINAL_STATUSES, iso_now
from .web_bridge import WebAttachment, WebBridge, WebInteraction
from .web_access import APPOINTED_COMMISSIONERS

log = logging.getLogger(__name__)
MEMBER_COMMANDS = {'registerteam', 'register', 'profile', 'roster', 'playersearch',
                   'requeststream', 'removemystream', 'mystreams', 'trade-block', 'transaction',
                   'leaderboard', 'season-history', 'help'}


def display_status(row):
    if row['status'] in FINAL_STATUSES:
        return 'Completed'
    if row.get('deadline_at') and datetime.fromisoformat(row['deadline_at']) < datetime.now(UTC):
        return 'Overdue'
    return 'Scheduled' if row['status'] == 'scheduled' else 'Not Scheduled'


def wire(value):
    """Snowflakes must be strings: JavaScript cannot represent Discord IDs exactly."""
    if isinstance(value, dict):
        return {k: str(v) if (k.endswith('_id') or k in {'guild_id', 'user_id'}) and v is not None else wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [wire(v) for v in value]
    return value


class WebsiteAPI:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.secret = os.getenv('WEB_API_SECRET', '')
        self.guild_ids = {int(x.strip()) for x in os.getenv('WEB_GUILD_IDS', '').split(',') if x.strip()}
        self.configured_guild_ids = set(self.guild_ids)
        self.registration_lock = asyncio.Lock()
        self.operator_ids = {int(x.strip()) for x in os.getenv('WEB_OPERATOR_IDS', '').split(',') if x.strip()}
        self.origin = os.getenv('WEB_ORIGIN', '').rstrip('/')
        if len(self.guild_ids) > 5:
            raise ValueError('WEB_GUILD_IDS supports at most five approved servers.')
        self.bridge = WebBridge(bot)
        self.nonces, self.locks = {}, {}
        self.runner = None
        self.worker = None
        self.app = web.Application(client_max_size=9 * 1024 * 1024, middlewares=[self.guard])
        self.app.router.add_get('/health', self.health)
        self.app.router.add_route('*', '/upload/{token}', self.upload)
        self.app.router.add_route('*', '/v1/servers', self.servers)
        self.app.router.add_route('*', '/v1/servers/{guild_id}/{resource}', self.resource)

    async def start(self):
        if not self.secret:
            return
        await self.refresh_guild_ids()
        if len(self.secret) < 32 or not self.origin.startswith('https://'):
            raise ValueError('Website API needs a 32+ character secret and HTTPS WEB_ORIGIN.')
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', int(os.getenv('SERVER_PORT', '8080'))).start()
        log.info('Website API started for %d approved servers', len(self.guild_ids))
        self.worker = asyncio.create_task(self.deliver_outbox(), name='website-discord-delivery')

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if self.runner:
            await self.runner.cleanup()

    async def deliver_outbox(self):
        while True:
            if self.bot.is_ready():
                await self.refresh_guild_ids()
                ids = sorted(self.guild_ids)
                rows = await self.db.fetchall('SELECT * FROM web_outbox WHERE delivered_at IS NULL AND guild_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY attempts,id LIMIT 20', ids) if ids else []
                for row in rows:
                    if row['guild_id'] not in self.guild_ids:
                        continue
                    try:
                        if row['kind'] == 'result_publish':
                            from .result_ui import _publish_final_result
                            matchup = await self.db.fetchone('SELECT * FROM matchups WHERE id=? AND guild_id=?', (row['entity_id'], row['guild_id']))
                            if not matchup or matchup['status'] not in FINAL_STATUSES:
                                raise ValueError('Final matchup is unavailable.')
                            if not await _publish_final_result(self.bot, matchup):
                                raise ValueError('Discord result publication could not complete.')
                        else:
                            settings = await self.db.settings(row['guild_id'])
                            channel = self.bot.get_channel(settings.get('audit_channel_id') or 0)
                            if not isinstance(channel, discord.TextChannel) or channel.guild.id != row['guild_id']:
                                raise ValueError('Commissioner audit channel is unavailable.')
                            await channel.send(json.loads(row['payload'])['text'], allowed_mentions=discord.AllowedMentions.none())
                        await self.db.execute('UPDATE web_outbox SET delivered_at=?,last_error=NULL WHERE id=?', (iso_now(), row['id']))
                    except Exception as exc:
                        await self.db.execute('UPDATE web_outbox SET attempts=attempts+1,last_error=? WHERE id=?', (str(exc)[:500], row['id']))
            await asyncio.sleep(30)

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.path not in {'/health'} and not request.path.startswith('/upload/'):
                body = await request.read()
                stamp, nonce = request.headers.get('X-Web-Time', ''), request.headers.get('X-Web-Nonce', '')
                actor = request.headers.get('X-Web-Actor', '0')
                self.nonces = {k: v for k, v in self.nonces.items() if v > time.time() - 60}
                canonical = '\n'.join([request.method, request.raw_path, stamp, nonce, actor, hashlib.sha256(body).hexdigest()])
                expected = hmac.new(self.secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
                if (not self.secret or not nonce or nonce in self.nonces or abs(time.time() - int(stamp or '0')) > 60
                        or not hmac.compare_digest(expected, request.headers.get('X-Web-Signature', ''))):
                    raise web.HTTPUnauthorized(text='Invalid API signature.')
                request['actor'] = int(actor)
                self.nonces[nonce] = time.time()
            return await handler(request)
        except web.HTTPException as exc:
            return web.json_response({'error': exc.text}, status=exc.status, headers=self.upload_error_headers(request))
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400, headers=self.upload_error_headers(request))
        except discord.HTTPException:
            return web.json_response({'error': 'Discord could not complete this action. Check bot access and retry.'}, status=502)
        except Exception:
            log.exception('Website request failed: %s', request.path)
            return web.json_response({'error': 'The action could not complete. Check commissioner activity before retrying.'}, status=500)

    async def health(self, request):
        return web.json_response({'ready': self.bot.is_ready()})

    def upload_error_headers(self, request):
        if request.path.startswith('/upload/') and request.headers.get('Origin') == self.origin:
            return {'Access-Control-Allow-Origin': self.origin, 'Vary': 'Origin'}
        return {}

    async def interaction(self, guild_id, actor, *, write=False):
        await self.refresh_guild_ids()
        if guild_id not in self.guild_ids:
            raise web.HTTPNotFound(text='Server is not enabled.')
        guild = self.bot.get_guild(guild_id)
        if guild is None or not self.bot.is_ready():
            raise web.HTTPServiceUnavailable(text='The league bot is reconnecting. Try again shortly.')
        if not actor:
            public = await self.db.fetchone('SELECT public_enabled FROM web_leagues WHERE guild_id=?', (guild_id,))
            if write or not public or not public['public_enabled']:
                raise web.HTTPForbidden(text='Sign in to view this league.')
            return guild, None, False
        try:
            # Fetch fresh membership, not a session-cached role or supplied permission.
            member = await guild.fetch_member(actor)
        except discord.NotFound:
            raise web.HTTPForbidden(text='You are no longer a member of this server.')
        settings = await self.db.settings(guild_id)
        channel = guild.get_channel(settings.get('audit_channel_id') or 0)
        interaction = WebInteraction(self.bot, guild, member, channel)
        admin = await is_commissioner(interaction, settings)
        interaction.web_admin = admin
        return guild, interaction, admin

    async def servers(self, request):
        await self.refresh_guild_ids()
        actor = request['actor']
        if request.method == 'POST':
            if actor not in APPOINTED_COMMISSIONERS:
                raise web.HTTPForbidden(text='Only the two appointed commissioners can register servers.')
            data = await request.json()
            raw_id = str(data.get('guildId', ''))
            if not raw_id.isascii() or not raw_id.isdecimal() or not 1 <= len(raw_id) <= 20 or not 0 < int(raw_id) < 2**63:
                raise ValueError('Enter a valid Discord server ID.')
            gid = int(raw_id)
            guild = self.bot.get_guild(gid)
            if not guild or not self.bot.is_ready():
                raise ValueError('The bot must already be in this server and online.')
            try:
                await guild.fetch_member(actor)
            except discord.NotFound:
                raise web.HTTPForbidden(text='You must be a member of the server you register.')
            async with self.registration_lock:
                async with self.db.connect() as conn:
                    await conn.execute('BEGIN IMMEDIATE')
                    rows = await (await conn.execute('SELECT guild_id FROM web_server_registry')).fetchall()
                    enabled = self.configured_guild_ids | {row['guild_id'] for row in rows}
                    if gid not in enabled and len(enabled) >= 5:
                        raise web.HTTPConflict(text='Five servers are already enabled. No additional server can be registered.')
                    if gid not in enabled:
                        await conn.execute('INSERT INTO web_server_registry VALUES (?,?,?)', (gid, actor, iso_now()))
                        await conn.execute('INSERT INTO audit_logs (guild_id,actor_id,action,details,created_at) VALUES (?,?,?,?,?)',
                            (gid, actor, 'website_server_registered', json.dumps({'guild_id': str(gid)}), iso_now()))
                    await conn.commit()
                await self.refresh_guild_ids()
        elif request.method != 'GET':
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
        servers = []
        for guild_id in sorted(self.guild_ids):
            try:
                guild, interaction, admin = await self.interaction(guild_id, request['actor'])
            except web.HTTPException:
                continue
            settings = await self.db.settings(guild_id)
            servers.append({'id': str(guild_id), 'name': guild.name, 'leagueName': settings['league_name'],
                            'icon': str(guild.icon.url) if guild.icon else None,
                            'role': 'commissioner' if admin else 'member' if interaction else 'public'})
        return web.json_response({'servers': servers, 'canRegisterServer': actor in APPOINTED_COMMISSIONERS,
                                  'enabledCount': len(self.guild_ids) if actor in APPOINTED_COMMISSIONERS else None})

    async def refresh_guild_ids(self):
        rows = await self.db.fetchall('SELECT guild_id FROM web_server_registry')
        ids = self.configured_guild_ids | {r['guild_id'] for r in rows}
        if len(ids) > 5:
            raise ValueError('At most five servers may be enabled across environment configuration and dashboard registrations.')
        self.guild_ids = ids
        return ids

    async def is_designated_commissioner(self, guild_id, actor):
        return bool(self.secret and actor in APPOINTED_COMMISSIONERS and guild_id in await self.refresh_guild_ids())

    async def resource(self, request):
        guild_id, resource = int(request.match_info['guild_id']), request.match_info['resource']
        guild, interaction, admin = await self.interaction(guild_id, request['actor'], write=request.method != 'GET')
        actor = request['actor']
        settings = await self.db.settings(guild_id)
        season = settings['season']
        if request.method == 'GET':
            if resource == 'snapshot':
                return web.json_response(wire(await self.snapshot(guild, actor, admin, settings)))
            if resource == 'players':
                rows = await self.db.fetchall('''SELECT external_player_id,team_name,full_name,position,overall,dev_trait,age,
                    jersey_number,attributes_json FROM roster_players WHERE guild_id=? AND season=?
                    AND (?='' OR team_name=?) AND full_name LIKE ? ORDER BY overall DESC,full_name LIMIT 100''',
                    (guild_id, season, request.query.get('team', ''), request.query.get('team', ''), '%' + request.query.get('q', '')[:100] + '%'))
                return web.json_response({'players': [dict(r) for r in rows]})
            if resource == 'commands':
                if not interaction:
                    raise web.HTTPForbidden(text='Sign in first.')
                commands = [self.bridge.command_schema(c) for c in self.bot.tree.get_commands()
                            if isinstance(c, discord.app_commands.Command) and (admin or c.name in MEMBER_COMMANDS)
                            and (c.name != 'backup' or actor in self.operator_ids)]
                return web.json_response({'commands': commands})
            if resource == 'download' and interaction:
                name, data = self.bridge.lookup(self.bridge.downloads, request.query.get('token'), (guild_id, actor), consume=True)
                if not admin or actor not in self.operator_ids:
                    raise web.HTTPForbidden(text='Platform operator access required for database backups.')
                return web.Response(body=data, headers={'Content-Type': 'application/octet-stream',
                    'Content-Disposition': "attachment; filename*=UTF-8''" + __import__('urllib.parse', fromlist=['quote']).quote(name)})
            raise web.HTTPNotFound(text='Unknown resource.')
        if request.method != 'POST':
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
        data = await request.json()
        if not interaction:
            raise web.HTTPForbidden(text='Sign in first.')
        if resource == 'upload-token':
            token = self.bridge.remember(self.bridge.pending, (guild_id, actor), ('upload', None, None), ttl=120)
            return web.json_response({'token': token})
        if resource == 'read-notifications':
            latest = await self.db.fetchone('SELECT COALESCE(MAX(id),0) AS id FROM web_events WHERE guild_id=?', (guild_id,))
            await self.db.execute('''INSERT INTO web_event_reads VALUES (?,?,?) ON CONFLICT(guild_id,user_id)
                DO UPDATE SET through_id=excluded.through_id''', (guild_id, actor, latest['id']))
            return web.json_response({'ok': True})
        key = request.headers.get('X-Request-Key', '')
        if len(key) < 16 or len(key) > 100:
            raise ValueError('An idempotency key is required.')
        lock = self.locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            digest = hashlib.sha256((resource + json.dumps(data, sort_keys=True)).encode()).hexdigest()
            old = await self.db.fetchone('SELECT * FROM web_requests WHERE guild_id=? AND actor_id=? AND request_key=?', (guild_id, actor, key))
            if old:
                if old['body_hash'] != digest:
                    raise web.HTTPConflict(text='Request key was reused with different data.')
                if old['response'] is None:
                    raise web.HTTPConflict(text='This action may already have run. Refresh activity before trying again.')
                return web.json_response(wire(json.loads(old['response'])))
            await self.db.execute('INSERT INTO web_requests VALUES (?,?,?,?,NULL,?)', (guild_id, actor, key, digest, iso_now()))
            result = await self.mutate(resource, data, interaction, admin, settings)
            await self.db.execute('UPDATE web_requests SET response=? WHERE guild_id=? AND actor_id=? AND request_key=?',
                                  (json.dumps(result), guild_id, actor, key))
            return web.json_response(wire(result))

    async def mutate(self, resource, data, interaction, admin, settings):
        guild_id, actor = interaction.guild_id, interaction.user.id
        if resource == 'command':
            name = str(data.get('name', ''))
            if name == 'backup' and actor not in self.operator_ids:
                raise web.HTTPForbidden(text='The database backup includes other servers. Platform operator access required.')
            if not admin and name not in MEMBER_COMMANDS:
                raise web.HTTPForbidden(text='Commissioner access required.')
            return await self.bridge.command(name, data.get('values', {}), interaction)
        if resource == 'control':
            return await self.bridge.respond(data['token'], interaction, data.get('values'))
        if resource == 'visibility':
            if not admin or not isinstance(data.get('public'), bool):
                raise web.HTTPForbidden(text='Commissioner access required.')
            await self.db.execute('INSERT INTO web_leagues VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET public_enabled=excluded.public_enabled', (guild_id, int(data['public'])))
            await self.db.audit(guild_id, actor, 'website_visibility', details={'public': data['public']})
            return {'ok': True}
        if resource == 'stream-review':
            from .stream_ui import StreamRequestReviewButton
            if not admin or data.get('action') not in {'approve', 'reject'}:
                raise web.HTTPForbidden(text='Commissioner access required.')
            row = await self.db.fetchone('SELECT * FROM stream_account_requests WHERE id=? AND guild_id=? AND season=?',
                                        (int(data['id']), guild_id, settings['season']))
            if not row or row['status'] != 'pending':
                raise ValueError('This stream request is no longer pending.')
            channel = interaction.guild.get_channel(row['audit_channel_id'] or 0)
            if not channel:
                raise ValueError('The original stream review channel is unavailable.')
            interaction.channel, interaction.channel_id = channel, channel.id
            interaction.message = await channel.fetch_message(row['audit_message_id'])
            await StreamRequestReviewButton(guild_id, row['id'], data['action']).callback(interaction)
            return self.bridge.serialize(interaction)
        if resource == 'record-adjustment':
            if not admin:
                raise web.HTTPForbidden(text='Commissioner access required.')
            reason, team = str(data.get('reason', '')).strip(), str(data.get('team', ''))
            if len(reason) < 10 or len(reason) > 1000 or data.get('season') != settings['season']:
                raise ValueError('Enter a reason of 10–1000 characters and reload the current season.')
            counts = [int(data[k]) for k in ('wins', 'losses', 'ties')]
            if min(counts) < 0 or max(counts) > 1000:
                raise ValueError('Records must be between 0 and 1000.')
            async with self.db.connect() as conn:
                await conn.execute('BEGIN IMMEDIATE')
                row = await (await conn.execute('SELECT * FROM teams WHERE guild_id=? AND season=? AND name=?', (guild_id, settings['season'], team))).fetchone()
                if not row:
                    raise ValueError('Team not found.')
                deltas = [counts[i] - row[k] for i, k in enumerate(('wins', 'losses', 'ties'))]
                await conn.execute('INSERT INTO web_record_adjustments (guild_id,season,team_name,actor_id,wins_delta,losses_delta,ties_delta,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (guild_id, settings['season'], team, actor, *deltas, reason, iso_now()))
                await conn.execute('UPDATE teams SET wins=?,losses=?,ties=? WHERE guild_id=? AND season=? AND name=?', (*counts, guild_id, settings['season'], team))
                await conn.execute('INSERT INTO web_outbox (guild_id,kind,payload,created_at) VALUES (?,?,?,?)',
                    (guild_id, 'audit_notice', json.dumps({'text': f'Website record adjustment · {team} · {counts[0]}–{counts[1]}–{counts[2]} · by <@{actor}>\nReason: {reason}'}), iso_now()))
                await conn.commit()
            await self.db.audit(guild_id, actor, 'record_adjusted', target_type='team', target_id=team, details={'deltas': deltas, 'reason': reason})
            return {'ok': True}
        if resource == 'create-matchup':
            from .channel_workflow import create_week_matchup_channels
            if not admin:
                raise web.HTTPForbidden(text='Commissioner access required.')
            if data.get('season') != settings['season']:
                raise ValueError('Season changed. Reload first.')
            week = int(data['week'])
            away, home = str(data['away']), str(data['home'])
            if not 1 <= week <= settings['regular_season_weeks'] or away == home:
                raise ValueError('Choose two different teams and a valid season week.')
            teams = await self.db.fetchall('SELECT name FROM teams WHERE guild_id=? AND season=? UNION SELECT team_name FROM franchises WHERE guild_id=? AND season=?', (guild_id, settings['season'], guild_id, settings['season']))
            if away not in {r[0] for r in teams} or home not in {r[0] for r in teams}:
                raise ValueError('Choose teams in this server’s current season.')
            duplicate = await self.db.fetchone('SELECT id FROM matchups WHERE guild_id=? AND season=? AND week=? AND ((away_team=? AND home_team=?) OR (away_team=? AND home_team=?))', (guild_id, settings['season'], week, away, home, home, away))
            if duplicate:
                raise ValueError('These teams already have a matchup in that week.')
            deadline = str(data.get('deadline') or settings.get('week_deadline_at') or '')
            if deadline:
                parsed = datetime.fromisoformat(deadline)
                if parsed.tzinfo is None:
                    raise ValueError('Deadline must include a timezone.')
                deadline = parsed.astimezone(UTC).isoformat()
            owners = {r['team_name']: r['user_id'] for r in await self.db.fetchall('SELECT team_name,user_id FROM profiles WHERE guild_id=? AND approved=1', (guild_id,))}
            now = iso_now()
            matchup_id = await self.db.execute('''INSERT INTO matchups (guild_id,season,week,external_key,away_team,home_team,away_user_id,home_user_id,deadline_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (guild_id, settings['season'], week, 'web-' + __import__('secrets').token_hex(12), away, home, owners.get(away), owners.get(home), deadline or None, now, now))
            await self.db.audit(guild_id, actor, 'matchup_created', target_type='matchup', target_id=str(matchup_id), details={'week': week, 'away': away, 'home': home})
            if week == settings['current_week']:
                await create_week_matchup_channels(interaction, self.db, season=settings['season'], week=week)
            await interaction.capture(f'{away} @ {home} created for Week {week}.')
            return self.bridge.serialize(interaction)
        if resource == 'matchup':
            from .result_ui import submit_result_from_message
            from .schedule_ui import MatchupMarkScheduledButton, ScheduleProposalModal, ScheduleDecisionButton
            from .commissioner_ui import CommissionerMatchupActionsView
            from .result_ui import CommissionerResultReviewView, OpponentResultDecisionButton
            from .channel_workflow import MatchupDisputeModal
            matchup = await self.db.fetchone('SELECT * FROM matchups WHERE id=? AND guild_id=? AND season=?', (int(data['id']), guild_id, settings['season']))
            if not matchup or not admin and actor not in (matchup['home_user_id'], matchup['away_user_id']):
                raise web.HTTPForbidden(text='Only assigned matchup owners or commissioners may manage this game.')
            interaction.channel = interaction.guild.get_channel(matchup['channel_id'] or 0) or interaction.channel
            interaction.channel_id = interaction.channel.id if interaction.channel else None
            action = data.get('action')
            if action == 'scheduled':
                await MatchupMarkScheduledButton(matchup['id']).callback(interaction)
            elif action == 'propose':
                await interaction.response.send_modal(ScheduleProposalModal(self.db, matchup['id'], settings['timezone']))
            elif action == 'dispute':
                await interaction.response.send_modal(MatchupDisputeModal(matchup['id']))
            elif action in {'accept', 'decline', 'counter'}:
                await ScheduleDecisionButton(action, matchup['id'], matchup['schedule_proposal_version']).callback(interaction)
            elif action in {'confirm-result', 'dispute-result'}:
                await OpponentResultDecisionButton('confirm' if action == 'confirm-result' else 'dispute', matchup['id'], matchup['result_submission_version']).callback(interaction)
            elif action == 'override' and admin:
                from .result_ui import _apply_result_decision
                reason = str(data.get('reason', '')).strip()
                scores = [int(data['awayScore']), int(data['homeScore'])]
                if matchup['status'] in FINAL_STATUSES:
                    raise ValueError('Reopen the final result before correcting its score.')
                if len(reason) < 10 or len(reason) > 1000 or min(scores) < 0 or max(scores) > 999 or scores[0] == scores[1]:
                    raise ValueError('Enter valid scores and a reason of 10–1000 characters.')
                async with self.db.connect() as conn:
                    await conn.execute('BEGIN IMMEDIATE')
                    cursor = await conn.execute('''UPDATE matchups SET away_score=?,home_score=?,status='result_pending',result_submitted_by=?,
                        result_submission_version=result_submission_version+1,result_submitted_at=?,updated_at=?
                        WHERE id=? AND guild_id=? AND status=? AND result_submission_version=?''',
                        (*scores, actor, iso_now(), iso_now(), matchup['id'], guild_id, matchup['status'], matchup['result_submission_version']))
                    if cursor.rowcount != 1:
                        raise web.HTTPConflict(text='This matchup changed. Reload before overriding.')
                    updated = await (await conn.execute('SELECT * FROM matchups WHERE id=?', (matchup['id'],))).fetchone()
                    await conn.commit()
                if not await _apply_result_decision(self.db, updated, 'approve', actor, web_override_reason=reason):
                    raise web.HTTPConflict(text='The result changed before approval.')
                await interaction.capture('Commissioner override finalized. Discord publication is queued; its delivery appears in activity.')
            elif action == 'submit':
                scores = [int(data['awayScore']), int(data['homeScore'])]
                if min(scores) < 0 or max(scores) > 999 or scores[0] == scores[1]:
                    raise ValueError('Enter different whole-number scores between 0 and 999.')
                attachment = self.bridge.lookup(self.bridge.uploads, data['evidence'], (guild_id, actor))
                if attachment.content_type not in {'image/png', 'image/jpeg', 'image/webp'}:
                    raise ValueError('Screenshot evidence must be PNG, JPEG or WebP.')
                ok, detail = await submit_result_from_message(self.bot, self.db, matchup, settings, actor, *scores, attachment)
                await interaction.capture(detail)
            elif action == 'manage' and admin:
                interaction.view = CommissionerMatchupActionsView(self.db, matchup, actor)
                if matchup['status'] in {'result_pending', 'issue_reported'}:
                    interaction.view = CommissionerResultReviewView(matchup['id'], matchup['result_submission_version'])
                from .result_ui import _result_embed
                await interaction.capture(embed=_result_embed(matchup), view=interaction.view)
            else:
                raise ValueError('Unknown matchup action.')
            return self.bridge.serialize(interaction)
        raise web.HTTPNotFound(text='Unknown action.')

    async def snapshot(self, guild, actor, admin, settings):
        gid, season = guild.id, settings['season']
        rows = await self.db.fetchall('''SELECT f.team_name AS name,f.abbreviation,f.external_team_id,
            COALESCE(t.wins,0) AS wins,COALESCE(t.losses,0) AS losses,COALESCE(t.ties,0) AS ties,
            p.user_id AS owner_id FROM franchises f LEFT JOIN teams t ON t.guild_id=f.guild_id AND t.season=f.season AND t.name=f.team_name
            LEFT JOIN profiles p ON p.guild_id=f.guild_id AND p.team_name=f.team_name AND p.approved=1
            WHERE f.guild_id=? AND f.season=? ORDER BY wins DESC,losses,name''', (gid, season))
        teams = [dict(r) for r in rows]
        if not teams:
            teams = [dict(r) for r in await self.db.fetchall('''SELECT t.name,t.wins,t.losses,t.ties,p.user_id AS owner_id FROM teams t
                LEFT JOIN profiles p ON p.guild_id=t.guild_id AND p.team_name=t.name AND p.approved=1
                WHERE t.guild_id=? AND t.season=? ORDER BY t.wins DESC,t.losses,t.name''', (gid, season))]
        for t in teams:
            member = guild.get_member(t.get('owner_id') or 0)
            t['owner_name'] = member.display_name if member else str(t.get('owner_id') or 'Available')
        matchups = [dict(r) for r in await self.db.fetchall('''SELECT id,week,away_team,home_team,away_user_id,home_user_id,
            away_score,home_score,scheduled_at,deadline_at,status,proposed_by,result_submitted_by,result_opponent_status,channel_id
            FROM matchups WHERE guild_id=? AND season=? ORDER BY week,id''', (gid, season))]
        for m in matchups:
            m['displayStatus'] = display_status(m)
            m['canManage'] = bool(actor and (admin or actor in (m['away_user_id'], m['home_user_id'])))
        events = [dict(r) for r in await self.db.fetchall('''SELECT id,kind,title,body,created_at FROM web_events WHERE guild_id=?
            AND (audience='public' OR ?=1 OR (audience='owners' AND (home_user_id=? OR away_user_id=?))) ORDER BY id DESC LIMIT 100''', (gid, int(admin), actor or -1, actor or -1))]
        read = await self.db.fetchone('SELECT through_id FROM web_event_reads WHERE guild_id=? AND user_id=?', (gid, actor))
        public = await self.db.fetchone('SELECT public_enabled FROM web_leagues WHERE guild_id=?', (gid,))
        streams = [dict(r) for r in await self.db.fetchall('SELECT * FROM web_live_streams WHERE guild_id=? AND last_seen_at>?', (gid, (datetime.now(UTC) - timedelta(seconds=max(self.bot.config.stream_poll_seconds * 3, 600))).isoformat()))]
        result = {'server': {'id': str(gid), 'name': guild.name}, 'role': 'commissioner' if admin else 'member' if actor else 'public',
                  'settings': {k: settings[k] for k in ('league_name', 'game', 'season', 'current_week', 'timezone', 'week_deadline_at', 'regular_season_weeks')},
                  'public': bool(public and public['public_enabled']), 'teams': teams, 'matchups': matchups, 'events': events,
                  'unread': sum(e['id'] > (read['through_id'] if read else 0) for e in events) if actor else 0, 'streams': streams,
                  'careers': [dict(r) for r in await self.db.fetchall('SELECT user_id,games,wins,losses,xp,points_for,points_against,championships FROM career_profiles WHERE guild_id=? ORDER BY xp DESC LIMIT 100', (gid,))]}
        if admin:
            result['deliveries'] = [dict(r) for r in await self.db.fetchall('SELECT id,kind,attempts,delivered_at,last_error,created_at FROM web_outbox WHERE guild_id=? ORDER BY id DESC LIMIT 50', (gid,))]
            result['claims'] = [dict(r) for r in await self.db.fetchall('SELECT user_id,team_name FROM profiles WHERE guild_id=? AND approved=0', (gid,))]
            result['audit'] = [dict(r) for r in await self.db.fetchall('SELECT * FROM audit_logs WHERE guild_id=? ORDER BY id DESC LIMIT 100', (gid,))]
            result['channels'] = [{'id': str(c.id), 'name': c.name} for c in guild.text_channels]
            result['roles'] = [{'id': str(r.id), 'name': r.name} for r in guild.roles]
            result['members'] = [{'id': str(m.id), 'name': m.display_name} for m in guild.members if not m.bot]
            result['streamRequests'] = [dict(r) for r in await self.db.fetchall('SELECT * FROM stream_account_requests WHERE guild_id=? AND season=? ORDER BY id DESC LIMIT 100', (gid, season))]
        return result

    async def upload(self, request):
        headers = {'Access-Control-Allow-Origin': self.origin, 'Vary': 'Origin'}
        if request.headers.get('Origin') != self.origin:
            raise web.HTTPForbidden(text='Invalid upload origin.')
        if request.method == 'OPTIONS':
            return web.Response(headers={**headers, 'Access-Control-Allow-Methods': 'POST', 'Access-Control-Allow-Headers': 'Content-Type'})
        token = request.match_info['token']
        entry = self.bridge.pending.get(token)
        if not entry:
            raise ValueError('Upload link expired.')
        owner = entry[1]
        if self.bridge.lookup(self.bridge.pending, token, owner)[0] != 'upload':
            raise ValueError('Invalid upload token.')
        self.bridge.lookup(self.bridge.pending, token, owner, consume=True)
        await self.interaction(*owner, write=True)
        if request.method != 'POST':
            raise web.HTTPMethodNotAllowed(request.method, ['POST'])
        reader = await request.multipart()
        part = await reader.next()
        if not part or not part.filename:
            raise ValueError('Choose a file.')
        data = bytearray()
        while chunk := await part.read_chunk():
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('File exceeds 8 MB.')
        content_type = part.headers.get('Content-Type', '')
        if content_type.startswith('image/'):
            valid = (content_type == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n') or content_type == 'image/jpeg' and data.startswith(b'\xff\xd8\xff') or content_type == 'image/webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP')
            if not valid:
                raise ValueError('Unsupported screenshot format.')
        else:
            bytes(data).decode('utf-8-sig')
            content_type = 'text/csv'
        key = self.bridge.remember(self.bridge.uploads, owner, WebAttachment(os.path.basename(part.filename), bytes(data), content_type))
        return web.json_response({'token': key}, headers=headers)

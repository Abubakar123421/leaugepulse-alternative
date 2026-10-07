"""Member stream requests and persistent commissioner review controls."""
from __future__ import annotations

import asyncio
import re

import aiohttp
import discord
from discord import app_commands

from .checks import require_commissioner
from .helpers import iso_now
from .stream_accounts import (
    decide_account_request, remove_account, request_account, resolve_youtube, twitch_login,
)

PLATFORMS = [app_commands.Choice(name='Twitch', value='twitch'),
             app_commands.Choice(name='YouTube', value='youtube')]


def request_embed(request):
    platform = request['platform']
    link = (f"https://www.twitch.tv/{request['account']}" if platform == 'twitch'
            else f"https://www.youtube.com/channel/{request['account']}")
    embed = discord.Embed(
        title=f"Streaming account request #{request['id']} · {request['status'].title()}",
        description=f"**Member:** <@{request['user_id']}>\n"
                    f"**Team:** {discord.utils.escape_markdown(request['team_name'])}\n"
                    f"**Platform:** {platform.title()}\n**Account:** {link}",
        color=discord.Color.gold() if request['status'] == 'pending' else discord.Color.green() if request['status'] == 'approved' else discord.Color.red(),
    )
    embed.set_footer(text='Only approved accounts with Gridiron Legends or Legacy in the live title are announced.')
    return embed


class StreamRequestReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'leaguebot:stream-review:(?P<guild_id>\d+):(?P<request_id>\d+):(?P<action>approve|reject)',
):
    def __init__(self, guild_id: int, request_id: int, action: str):
        self.guild_id, self.request_id, self.action = guild_id, request_id, action
        super().__init__(discord.ui.Button(
            label='Approve' if action == 'approve' else 'Reject',
            style=discord.ButtonStyle.success if action == 'approve' else discord.ButtonStyle.danger,
            custom_id=f'leaguebot:stream-review:{guild_id}:{request_id}:{action}',
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match['guild_id']), int(match['request_id']), match['action'])

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild_id != self.guild_id:
            await interaction.response.send_message('This request belongs to another server.', ephemeral=True)
            return
        db = interaction.client.db
        settings = await db.settings(self.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await db.fetchone('SELECT * FROM stream_account_requests WHERE id=? AND guild_id=?', (self.request_id, self.guild_id))
        if (not request or not interaction.message
                or request['audit_channel_id'] != interaction.channel_id
                or request['audit_message_id'] != interaction.message.id):
            await interaction.followup.send('This is not the original audit review message.', ephemeral=True)
            return
        if self.action == 'approve' and not interaction.guild.get_member(request['user_id']):
            await interaction.followup.send('The member is no longer in this server. Reject the request instead.', ephemeral=True)
            return
        try:
            resolved = await decide_account_request(
                db, self.guild_id, self.request_id, approve=self.action == 'approve', actor_id=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        embed = request_embed(resolved)
        embed.add_field(name='Decided by', value=interaction.user.mention)
        try:
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        member = interaction.guild.get_member(request['user_id'])
        if member:
            try:
                await member.send(f"Your {request['platform'].title()} account request in {interaction.guild.name} was {resolved['status']}.")
            except discord.HTTPException:
                pass
        await interaction.followup.send(f"Request #{self.request_id} {resolved['status']}.", ephemeral=True)


async def log_stream_removal(bot, db, guild_id, user_id, platform, actor_id):
    settings = await db.settings(guild_id)
    channel = bot.get_channel(settings.get('audit_channel_id') or 0)
    if isinstance(channel, discord.TextChannel) and channel.guild.id == guild_id:
        try:
            await channel.send(
                f"Streaming account removed · <@{user_id}> · {platform.title()} · by <@{actor_id}>. Pending requests for that platform were cancelled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass


def register_member_stream_commands(bot):
    tree, db = bot.tree, bot.db

    @tree.command(name='requeststream', description='Request commissioner approval for your Twitch or YouTube account.')
    @app_commands.choices(platform=PLATFORMS)
    async def request_stream(interaction: discord.Interaction, platform: app_commands.Choice[str], account: str):
        if not interaction.guild:
            await interaction.response.send_message('Use this command inside your league server.', ephemeral=True)
            return
        settings = await db.settings(interaction.guild_id)
        profile = await db.fetchone('SELECT * FROM profiles WHERE guild_id=? AND user_id=? AND approved=1', (interaction.guild_id, interaction.user.id))
        if not profile:
            await interaction.response.send_message('Only members with an approved team can request streaming accounts.', ephemeral=True)
            return
        audit = bot.get_channel(settings.get('audit_channel_id') or 0)
        if not isinstance(audit, discord.TextChannel) or audit.guild.id != interaction.guild_id:
            await interaction.response.send_message('A commissioner must configure /setauditchannel first.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if platform.value == 'twitch':
                canonical = twitch_login(account)
            elif platform.value == 'youtube':
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                    canonical = await resolve_youtube(session, account, bot.config.youtube_api_key)
            else:
                raise ValueError('Choose Twitch or YouTube.')
            request_id = await request_account(db, interaction.guild_id, interaction.user.id, platform.value, canonical)
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(str(exc) if isinstance(exc, ValueError) else 'Account verification unavailable. Please try again.', ephemeral=True)
            return
        request = await db.fetchone('SELECT * FROM stream_account_requests WHERE id=? AND guild_id=?', (request_id, interaction.guild_id))
        view = discord.ui.View(timeout=None)
        view.add_item(StreamRequestReviewButton(interaction.guild_id, request_id, 'approve'))
        view.add_item(StreamRequestReviewButton(interaction.guild_id, request_id, 'reject'))
        role_id = settings.get('commissioner_role_id')
        try:
            message = await audit.send(
                f'<@&{role_id}>' if role_id else None,
                embed=request_embed(request), view=view,
                allowed_mentions=discord.AllowedMentions(users=False, roles=True, everyone=False),
            )
        except discord.HTTPException:
            await db.execute("UPDATE stream_account_requests SET status='delivery_failed',updated_at=? WHERE id=? AND guild_id=? AND status='pending'", (iso_now(), request_id, interaction.guild_id))
            await interaction.followup.send('Could not post to commissioner audit. Ask a commissioner to check my channel permissions, then submit again.', ephemeral=True)
            return
        await db.execute('UPDATE stream_account_requests SET audit_channel_id=?,audit_message_id=?,updated_at=? WHERE id=? AND guild_id=?', (audit.id, message.id, iso_now(), request_id, interaction.guild_id))
        await db.audit(interaction.guild_id, interaction.user.id, 'stream_account_requested', target_type='member', target_id=str(interaction.user.id), details={'request_id': request_id, 'platform': platform.value})
        await interaction.followup.send(f"Your {platform.name} request #{request_id} is pending commissioner approval. Your existing account stays active until approval. Use /mystreams to check status.", ephemeral=True)

    @tree.command(name='removemystream', description='Remove your Twitch or YouTube account and cancel its pending request.')
    @app_commands.choices(platform=PLATFORMS)
    async def remove_my_stream(interaction: discord.Interaction, platform: app_commands.Choice[str]):
        if not interaction.guild:
            await interaction.response.send_message('Use this command inside your league server.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await remove_account(db, interaction.guild_id, interaction.user.id, platform.value, actor_id=interaction.user.id)
        await log_stream_removal(bot, db, interaction.guild_id, interaction.user.id, platform.value, interaction.user.id)
        await interaction.followup.send(f'Your {platform.name} account was removed and its pending request cancelled. No commissioner approval is needed.', ephemeral=True)

    @tree.command(name='mystreams', description='View your approved streaming accounts and latest request decisions.')
    async def my_streams(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message('Use this command inside your league server.', ephemeral=True)
            return
        profile = await db.fetchone('SELECT * FROM profiles WHERE guild_id=? AND user_id=?', (interaction.guild_id, interaction.user.id))
        lines = [f"Twitch: {profile['twitch'] or 'None'}" if profile else 'Twitch: None',
                 f"YouTube: {profile['youtube'] or 'None'}" if profile else 'YouTube: None']
        settings = await db.settings(interaction.guild_id)
        for platform in ('twitch', 'youtube'):
            request = await db.fetchone('SELECT * FROM stream_account_requests WHERE guild_id=? AND season=? AND user_id=? AND platform=? ORDER BY id DESC LIMIT 1', (interaction.guild_id, settings['season'], interaction.user.id, platform))
            if request:
                lines.append(f"{platform.title()} request #{request['id']}: {request['status']}")
        await interaction.response.send_message('\n'.join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

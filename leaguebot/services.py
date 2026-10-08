from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from pathlib import Path

import aiohttp
import discord

from .db import Database
from .helpers import FINAL_STATUSES, iso_now, utcnow
from .team_emojis import team_label

log = logging.getLogger(__name__)


class ReminderService:
    def __init__(self, bot: discord.Client, db: Database, interval: int):
        self.bot = bot
        self.db = db
        self.interval = interval
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.run(), name="league-reminders")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Reminder service tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        rows = await self.db.fetchall(
            """SELECT m.*,g.audit_channel_id,g.commissioner_role_id
               FROM matchups m JOIN guild_settings g ON g.guild_id=m.guild_id
               WHERE m.scheduled_at IS NULL
                 AND m.status NOT IN
                   ('scheduled','result_pending','complete','force_home','force_away','fair_sim')"""
        )
        now = utcnow()
        for row in rows:
            if not row["deadline_at"] or not row["channel_id"]:
                continue
            deadline = _dt(row["deadline_at"])
            remaining = deadline - now
            if remaining <= timedelta(0):
                slot = "unscheduled_overdue"
                label = "The advance deadline has passed"
            else:
                last = await self.db.fetchone(
                    """SELECT delivered_at FROM reminder_deliveries
                       WHERE matchup_id=? AND milestone LIKE 'unscheduled_%'
                         AND milestone != 'unscheduled_overdue'
                       ORDER BY delivered_at DESC LIMIT 1""",
                    (row["id"],),
                )
                last_reminder = _dt(last["delivered_at"] if last else row["created_at"])
                if now - last_reminder < timedelta(hours=12):
                    continue
                slot = f"unscheduled_12h_{int(now.timestamp())}"
                label = f"Time remaining: {max(1, int(remaining.total_seconds() // 3600))} hour(s)"
            if not await self._claim(row["id"], slot):
                continue
            channel = self.bot.get_channel(row["channel_id"])
            guild = self.bot.get_guild(row["guild_id"] )
            away_display = (
                await team_label(self.db, guild, row["season"], row["away_team"] )
                if guild else row["away_team"]
            )
            home_display = (
                await team_label(self.db, guild, row["season"], row["home_team"] )
                if guild else row["home_team"]
            )
            owners = [row["away_user_id"], row["home_user_id"]]
            mentions = " ".join(f"<@{value}>" for value in owners if value)
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    f"⏰ {mentions}\n\nYour Week {row['week']} matchup has not been scheduled.\n"
                    f"{away_display} at {home_display}\n"
                    f"Advance deadline: <t:{int(deadline.timestamp())}:F>\n{label}\n\n"
                    "Use **Mark as Scheduled** on the matchup card once the game time is agreed.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            if any(value is None for value in owners):
                audit = guild.get_channel(row["audit_channel_id"] or 0) if guild else None
                if isinstance(audit, discord.TextChannel):
                    await audit.send(
                        f"⚠️ Matchup #{row['id']} ({row['away_team']} @ {row['home_team']}) "
                        "has an unassigned owner and cannot notify both players."
                    )

    async def _claim(self, matchup_id: int, slot: str) -> bool:
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                """INSERT OR IGNORE INTO reminder_deliveries
                   (matchup_id,milestone,delivered_at) VALUES (?,?,?)""",
                (matchup_id, slot, iso_now()),
            )
            await conn.commit()
            return cursor.rowcount == 1

class WeekRolloverService:
    def __init__(self, bot: discord.Client, db: Database, interval: int = 60):
        self.bot = bot
        self.db = db
        self.interval = max(60, interval)
        self.task: asyncio.Task | None = None
        self.content_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.db.execute(
            """UPDATE week_rollovers SET status='failed',error_text='Interrupted by bot restart'
               WHERE status='running'"""
        )
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.run(), name="league-week-rollovers")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
        for content_task in list(self.content_tasks):
            content_task.cancel()
        if self.content_tasks:
            await asyncio.gather(*self.content_tasks, return_exceptions=True)

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Week rollover service tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        # Retry old weekly cleanup after a temporary Discord permission/API failure.
        active = await self.db.fetchall(
            "SELECT guild_id,season,current_week FROM guild_settings WHERE current_week>1"
        )
        from .channel_workflow import lock_and_delete_week_channels
        for row in active:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            old_weeks = await self.db.fetchall(
                """SELECT week FROM week_categories WHERE guild_id=? AND season=? AND week<?
                   ORDER BY week""",
                (row["guild_id"], row["season"], row["current_week"]),
            )
            for old in old_weeks:
                await lock_and_delete_week_channels(
                    guild, self.db, row["season"], old["week"]
                )
        due = await self.db.fetchall(
            """SELECT * FROM guild_settings WHERE auto_week_rollover=1
               AND week_deadline_at IS NOT NULL AND week_deadline_at<=?""",
            (iso_now(),),
        )
        for settings in due:
            await self.rollover(
                settings["guild_id"], expected_week=settings["current_week"], actor_id=0
            )

    async def rollover(
        self, guild_id: int, *, expected_week: int | None = None, actor_id: int = 0
    ) -> tuple[bool, str]:
        settings = await self.db.settings(guild_id)
        week = settings["current_week"]
        season = settings["season"]
        if expected_week is not None and week != expected_week:
            return False, "The active week changed before rollover."
        total_weeks = settings.get("regular_season_weeks") or 18
        final_week = week >= total_weeks
        next_week = None if final_week else week + 1
        now = iso_now()
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT status FROM week_rollovers WHERE guild_id=? AND season=? AND from_week=?",
                (guild_id, season, week),
            )
            existing = await cursor.fetchone()
            if existing and existing["status"] in {"complete", "running"}:
                await conn.rollback()
                return False, "This week rollover is already complete or in progress."
            await conn.execute(
                """INSERT INTO week_rollovers
                   (guild_id,season,from_week,to_week,status,started_at)
                   VALUES (?,?,?,?, 'running',?) ON CONFLICT(guild_id,season,from_week)
                   DO UPDATE SET status='running',error_text=NULL,started_at=excluded.started_at""",
                (guild_id, season, week, next_week, now),
            )
            await conn.commit()
        guild = self.bot.get_guild(guild_id)
        if not guild:
            await self._fail(guild_id, season, week, "Guild is unavailable to the bot.")
            return False, "Guild is unavailable."
        unresolved_rows = await self.db.fetchall(
            """SELECT id,away_team,home_team,status FROM matchups
               WHERE guild_id=? AND season=? AND week=? AND status NOT IN
               ('complete','force_home','force_away','fair_sim') ORDER BY id""",
            (guild_id, season, week),
        )
        channel_errors: list[str] = []
        created = 0
        if next_week is not None:
            count = await self.db.fetchone(
                "SELECT COUNT(*) AS total FROM matchups WHERE guild_id=? AND season=? AND week=?",
                (guild_id, season, next_week),
            )
            if not count or not count["total"]:
                await self._fail(guild_id, season, week, f"Week {next_week} has no imported fixtures.")
                return False, f"Week {next_week} has no imported fixtures."
            from .channel_workflow import create_week_matchup_channels
            created, channel_errors = await create_week_matchup_channels(
                SimpleNamespace(guild=guild, client=self.bot), self.db,
                season=season, week=next_week,
            )
            if channel_errors:
                detail = "; ".join(channel_errors[:5])
                await self._fail(guild_id, season, week, detail)
                return False, (
                    f"Week {next_week} could not be fully opened. The current week was kept; "
                    f"retry after fixing Discord permissions. {detail}"
                )
        await self._audit_rollover(
            guild, settings, week, next_week, unresolved_rows, created, channel_errors
        )
        from .channel_workflow import lock_and_delete_week_channels
        cleanup_errors = await lock_and_delete_week_channels(guild, self.db, season, week)
        if final_week:
            await self.db.update_settings(
                guild_id, auto_week_rollover=0, week_started_at=None, week_deadline_at=None
            )
        else:
            old_deadline = _dt(settings["week_deadline_at"]) if settings.get("week_deadline_at") else utcnow()
            new_deadline = old_deadline + timedelta(days=7)
            await self.db.update_settings(
                guild_id, current_week=next_week, week_started_at=old_deadline.isoformat(),
                week_deadline_at=new_deadline.isoformat(),
            )
        await self.db.execute(
            """UPDATE week_rollovers SET status='complete',unresolved_count=?,error_text=?,
               completed_at=? WHERE guild_id=? AND season=? AND from_week=?""",
            (
                len(unresolved_rows), "; ".join(cleanup_errors + channel_errors)[:1000] or None,
                iso_now(), guild_id, season, week,
            ),
        )
        await self.db.audit(
            guild_id, actor_id, "automatic_week_rollover" if actor_id == 0 else "manual_week_rollover",
            details={
                "from": week, "to": next_week, "unresolved": len(unresolved_rows),
                "channels_created": created, "cleanup_errors": cleanup_errors,
            },
        )
        from .weekly_content import publish_weekly_recap
        task = asyncio.create_task(
            publish_weekly_recap(
                self.bot, self.db, guild_id, season, week,
                create=True, regenerate_ai=True,
            ),
            name=f"weekly-recap-{guild_id}-{season}-{week}",
        )
        self.content_tasks.add(task)
        task.add_done_callback(self.content_tasks.discard)
        return True, (
            f"Week {week} closed; " + (f"Week {next_week} opened." if next_week else "regular season completed.")
        )

    async def _fail(self, guild_id: int, season: str, week: int, error: str) -> None:
        await self.db.execute(
            """UPDATE week_rollovers SET status='failed',error_text=?
               WHERE guild_id=? AND season=? AND from_week=?""",
            (error[:1000], guild_id, season, week),
        )

    async def _audit_rollover(
        self, guild, settings, week, next_week, unresolved, created, errors
    ) -> None:
        audit = guild.get_channel(settings.get("audit_channel_id") or 0)
        if not isinstance(audit, discord.TextChannel):
            return
        unresolved_text = "\n".join(
            f"#{row['id']} · {row['away_team']} @ {row['home_team']} · {row['status']}"
            for row in unresolved[:20]
        ) or "None"
        embed = discord.Embed(
            title=f"Week {week} Automatic Rollover",
            description=(
                f"Next week: **{next_week if next_week else 'Regular season complete'}**\n"
                f"New matchup channels: **{created}**\n"
                f"Unresolved games retained in database: **{len(unresolved)}**"
            ),
            color=discord.Color.gold() if unresolved or errors else discord.Color.green(),
        )
        embed.add_field(name="Unresolved", value=unresolved_text[:1024], inline=False)
        if errors:
            embed.add_field(name="Discord warnings", value="\n".join(errors[:8])[:1024], inline=False)
        await audit.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

class StreamService:
    def __init__(
        self, bot: discord.Client, db: Database, interval: int,
        twitch_client_id: str | None, twitch_secret: str | None, youtube_key: str | None,
        *, youtube_search_daily_limit: int = 90,
    ):
        self.bot, self.db = bot, db
        self.interval = max(60, interval)
        self.twitch_client_id, self.twitch_secret = twitch_client_id, twitch_secret
        self.youtube_key = youtube_key
        self.youtube_search_daily_limit = max(1, youtube_search_daily_limit)
        self.task: asyncio.Task | None = None
        self._twitch_token: str | None = None

    def start(self) -> None:
        if not self.task or self.task.done():
            if (self.twitch_client_id and self.twitch_secret) or self.youtube_key:
                self.task = asyncio.create_task(self.run(), name="league-stream-alerts")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while not self.bot.is_closed():
                try:
                    await self.tick(session)
                except asyncio.CancelledError:
                    return
                except Exception:
                    # Do not log HTTP exception text: Google request URLs contain the API key.
                    log.warning("Stream service tick failed")
                await asyncio.sleep(self.interval)

    async def tick(self, session: aiohttp.ClientSession) -> None:
        for platform in ("twitch", "youtube"):
            if platform == "twitch" and not (self.twitch_client_id and self.twitch_secret):
                continue
            if platform == "youtube" and not self.youtube_key:
                continue
            try:
                await getattr(self, f"_tick_{platform}")(session)
            except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException, ValueError, KeyError):
                log.warning("%s stream check failed; will retry", platform)

    async def _profiles(self, platform: str):
        import json
        rows = await self.db.fetchall(
            f"""SELECT p.*,g.streams_channel_id,g.season,g.current_week,g.features
                FROM profiles p JOIN guild_settings g ON g.guild_id=p.guild_id
                WHERE p.approved=1 AND p.{platform} IS NOT NULL AND p.{platform}!=''
                  AND g.streams_channel_id IS NOT NULL"""
        )
        return [row for row in rows if json.loads(row["features"]).get("streams", True)]

    async def _tick_twitch(self, session: aiohttp.ClientSession) -> None:
        from .stream_accounts import twitch_login
        profiles = await self._profiles("twitch")
        accounts = {}
        for profile in profiles:
            try:
                accounts.setdefault(twitch_login(profile["twitch"]), []).append(profile)
            except ValueError:
                continue
        if not accounts:
            return
        if not self._twitch_token:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                data={"client_id": self.twitch_client_id, "client_secret": self.twitch_secret,
                      "grant_type": "client_credentials"},
            ) as response:
                response.raise_for_status()
                self._twitch_token = (await response.json())["access_token"]
        names = list(accounts)
        for offset in range(0, len(names), 100):
            async with session.get(
                "https://api.twitch.tv/helix/streams",
                params=[("user_login", name) for name in names[offset:offset + 100]] + [("first", "100")],
                headers={"Client-ID": self.twitch_client_id, "Authorization": f"Bearer {self._twitch_token}"},
            ) as response:
                if response.status == 401:
                    self._twitch_token = None
                    return
                response.raise_for_status()
                streams = (await response.json()).get("data", [])
            for stream in streams:
                if stream.get("type") != "live":
                    continue
                name = stream.get("user_login", "").lower()
                for profile in accounts.get(name, []):
                    await self._announce(profile, "twitch", name, stream["id"],
                                         stream.get("title", ""), f"https://www.twitch.tv/{name}")

    async def _reserve_youtube_search(self, *, first_page: bool) -> bool:
        from zoneinfo import ZoneInfo
        now = utcnow()
        quota_day = now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("INSERT OR IGNORE INTO youtube_search_usage (quota_day) VALUES (?)", (quota_day,))
            cursor = await conn.execute("SELECT * FROM youtube_search_usage WHERE quota_day=?", (quota_day,))
            state = await cursor.fetchone()
            if state["requests"] >= self.youtube_search_daily_limit:
                return False
            # Persist pacing across restarts and share one search across all registered channels.
            cursor = await conn.execute("SELECT last_checked_at FROM youtube_search_usage WHERE last_checked_at IS NOT NULL ORDER BY last_checked_at DESC LIMIT 1")
            last = await cursor.fetchone()
            spacing = max(self.interval, 86400 / self.youtube_search_daily_limit)
            if first_page and last and (now - _dt(last["last_checked_at"])).total_seconds() < spacing:
                return False
            await conn.execute("UPDATE youtube_search_usage SET requests=requests+1,last_checked_at=? WHERE quota_day=?", (now.isoformat(), quota_day))
            await conn.commit()
        return True

    async def _tick_youtube(self, session: aiohttp.ClientSession) -> None:
        from .stream_accounts import resolve_youtube
        profiles = await self._profiles("youtube")
        accounts = {}
        for profile in profiles:
            try:
                channel_id = await resolve_youtube(session, profile["youtube"], self.youtube_key)
                accounts.setdefault(channel_id, []).append(profile)
                # Canonicalize legacy handles once; no repeated account-resolution calls.
                canonical = f"https://www.youtube.com/channel/{channel_id}"
                if profile["youtube"] != canonical:
                    await self.db.execute("UPDATE profiles SET youtube=? WHERE guild_id=? AND user_id=? AND youtube=?", (canonical, profile["guild_id"], profile["user_id"], profile["youtube"]))
            except ValueError:
                continue
        if not accounts:
            return
        page = None
        while await self._reserve_youtube_search(first_page=page is None):
            params = {"part": "snippet", "eventType": "live", "type": "video",
                      "q": '"Gridiron Legends"|Legacy', "maxResults": 50, "key": self.youtube_key}
            if page:
                params["pageToken"] = page
            async with session.get("https://www.googleapis.com/youtube/v3/search", params=params) as response:
                if response.status in (400, 403):
                    log.warning("YouTube search rejected; check API credentials and quota")
                    return
                response.raise_for_status()
                result = await response.json()
            for item in result.get("items", []):
                snippet = item.get("snippet", {})
                channel_id = snippet.get("channelId")
                if snippet.get("liveBroadcastContent") != "live":
                    continue
                live_id = item.get("id", {}).get("videoId")
                if not live_id:
                    continue
                from html import unescape
                title = unescape(snippet.get("title", ""))
                for profile in accounts.get(channel_id, []):
                    await self._announce(profile, "youtube", channel_id, live_id,
                                         title, f"https://www.youtube.com/watch?v={live_id}")
            page = result.get("nextPageToken")
            if not page:
                break

    async def _announce(self, profile, platform: str, account: str, live_id: str, title: str, url: str) -> None:
        normalized_title = title.casefold()
        if "gridiron legends" not in normalized_title and "legacy" not in normalized_title:
            return
        channel = self.bot.get_channel(profile["streams_channel_id"])
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != profile["guild_id"]:
            return
        # Honor sessions already announced by the previous service during upgrades.
        old = await self.db.fetchone(
            "SELECT live_id FROM stream_alert_state WHERE guild_id=? AND platform=? AND channel_key=?",
            (profile["guild_id"], platform, account),
        )
        if old and old["live_id"] == live_id:
            return
        key = (profile["guild_id"], platform, account, str(live_id))
        embed = discord.Embed(title=f"Live on {platform.title()} - League Stream", url=url,
                              description=discord.utils.escape_markdown(title)[:4096], color=discord.Color.purple())
        embed.add_field(name="Member / Team", value=f"<@{profile['user_id']}> · {discord.utils.escape_markdown(profile['team_name'])}"[:1024], inline=False)
        embed.add_field(name="Platform", value=platform.title())
        embed.add_field(name="Watch live", value=f"[Open stream]({url})", inline=False)
        matchup = await self.db.fetchone(
            """SELECT away_team,home_team FROM matchups
               WHERE guild_id=? AND season=? AND week=? AND (away_user_id=? OR home_user_id=?)
               ORDER BY id LIMIT 1""",
            (profile["guild_id"], profile["season"], profile["current_week"], profile["user_id"], profile["user_id"]),
        )
        if matchup:
            embed.add_field(name=f"Current matchup · Week {profile['current_week']}",
                            value=discord.utils.escape_markdown(f"{matchup['away_team']} @ {matchup['home_team']}")[:1024], inline=False)
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO stream_notifications (guild_id,platform,channel_key,live_id,created_at) VALUES (?,?,?,?,?)",
                (*key, iso_now()),
            )
            await conn.commit()
            if cursor.rowcount != 1:
                return
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound):
            # Definitive rejection: safe to retry after permissions/routing are fixed.
            await self.db.execute("DELETE FROM stream_notifications WHERE guild_id=? AND platform=? AND channel_key=? AND live_id=?", key)
            raise
        # An ambiguous transport failure retains the reservation to avoid duplicate posts.
        await self.db.execute("UPDATE stream_notifications SET message_id=? WHERE guild_id=? AND platform=? AND channel_key=? AND live_id=?", (message.id, *key))


def _dt(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value)


async def make_backup(db: Database, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"leaguebot-{utcnow():%Y%m%d-%H%M%S}.sqlite3"
    async with db.connect() as source:
        import aiosqlite
        async with aiosqlite.connect(destination) as target:
            await source.backup(target)
    return destination

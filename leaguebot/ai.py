from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import discord

from .db import Database
from .helpers import iso_now

log = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
_MASS_MENTIONS = re.compile(r"@(everyone|here)|<@&\d+>", re.IGNORECASE)


def sanitize_ai_text(value: str, limit: int = 3900) -> str:
    clean = _MASS_MENTIONS.sub(lambda m: "@\u200b" + m.group(0).lstrip("@<").split(">", 1)[0], value)
    clean = clean.replace("```", "'''").strip()
    return clean[:limit] or "AI content was generated, but it was empty."


async def deterministic_rankings(db: Database, guild_id: int, season: str, week: int) -> list[dict[str, Any]]:
    teams = await db.fetchall(
        "SELECT name,wins,losses,ties FROM teams WHERE guild_id=? AND season=?", (guild_id, season)
    )
    history = await db.fetchall(
        """SELECT * FROM game_history WHERE guild_id=? AND season=? AND week<=?
           AND decision_type='complete' ORDER BY week DESC,id DESC""",
        (guild_id, season, week),
    )
    ranked = []
    for team in teams:
        games = team["wins"] + team["losses"] + team["ties"]
        win_pct = (team["wins"] + 0.5 * team["ties"]) / games if games else 0.0
        pd = recent = 0
        recent_games = []
        for game in history:
            if game["away_team"].casefold() == team["name"].casefold():
                pd += (game["away_score"] or 0) - (game["home_score"] or 0)
                recent_games.append(1 if (game["away_score"] or 0) > (game["home_score"] or 0) else 0)
            elif game["home_team"].casefold() == team["name"].casefold():
                pd += (game["home_score"] or 0) - (game["away_score"] or 0)
                recent_games.append(1 if (game["home_score"] or 0) > (game["away_score"] or 0) else 0)
        recent = sum(recent_games[:3])
        ranked.append({
            "team": team["name"], "wins": team["wins"], "losses": team["losses"],
            "ties": team["ties"], "win_pct": win_pct, "point_diff": pd, "recent_wins": recent,
        })
    ranked.sort(key=lambda r: (-r["win_pct"], -r["point_diff"], -r["recent_wins"], r["team"].casefold()))
    for index, row in enumerate(ranked, 1):
        row["rank"] = index
    return ranked


class AIService:
    def __init__(self, client: discord.Client, db: Database, config):
        self.client = client
        self.db = db
        self.config = config
        self._semaphore = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task] = set()
        self._genai_client = None
        if config.ai_enabled and config.gemini_api_key:
            try:
                from google import genai
                self._genai_client = genai.Client(api_key=config.gemini_api_key)
            except ImportError:
                log.warning("google-genai is not installed; AI commands will report unavailable")

    @property
    def available(self) -> bool:
        return bool(self.config.ai_enabled and self.config.gemini_api_key and self._genai_client)

    async def start(self) -> None:
        # A process can stop between reserving and posting. Mark those jobs retryable;
        # posted jobs remain immutable and therefore cannot duplicate on restart.
        await self.db.execute(
            """UPDATE ai_jobs SET status='failed',error_text='Interrupted by bot restart',updated_at=?
               WHERE status='running'""",
            (iso_now(),),
        )

    def enqueue(self, **kwargs) -> None:
        task = asyncio.create_task(self.generate_and_post(**kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _reserve_daily_request(self, guild_id: int) -> bool:
        day = datetime.now(UTC).date().isoformat()
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT requests FROM ai_daily_usage WHERE usage_date=? AND guild_id=?", (day, guild_id)
            )
            row = await cursor.fetchone()
            count = row["requests"] if row else 0
            if count >= self.config.ai_daily_limit:
                await conn.rollback()
                return False
            await conn.execute(
                """INSERT INTO ai_daily_usage (usage_date,guild_id,requests,updated_at)
                   VALUES (?,?,1,?) ON CONFLICT(usage_date,guild_id) DO UPDATE SET
                   requests=requests+1,updated_at=excluded.updated_at""",
                (day, guild_id, iso_now()),
            )
            await conn.commit()
        return True

    async def generate(self, guild_id: int, prompt: str) -> str:
        if not self.available:
            raise RuntimeError("Gemini AI is not configured or google-genai is not installed.")
        if not await self._reserve_daily_request(guild_id):
            raise RuntimeError("This server has reached today's AI request ceiling.")
        async with self._semaphore:
            last_error = None
            for attempt in range(3):
                try:
                    response = await self._genai_client.aio.models.generate_content(
                        model=self.config.gemini_model,
                        contents=prompt,
                    )
                    return sanitize_ai_text(response.text or "")
                except Exception as exc:
                    last_error = exc
                    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                    if code != 429 and "429" not in str(exc):
                        break
                    await asyncio.sleep(2 ** attempt)
            raise RuntimeError(f"Gemini generation failed: {type(last_error).__name__}") from last_error

    async def generate_and_post(
        self, *, guild_id: int, season: str, source_key: str, kind: str,
        prompt: str, channel_id: int, title: str,
    ) -> tuple[bool, str]:
        settings = await self.db.settings(guild_id)
        if not self.available or not settings.get("ai_enabled", 1):
            return False, "AI is disabled or unavailable."
        now = iso_now()
        async with self.db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """SELECT status FROM ai_jobs WHERE guild_id=? AND season=? AND source_key=?
                   AND kind=? AND prompt_version=?""",
                (guild_id, season, source_key, kind, PROMPT_VERSION),
            )
            existing = await cursor.fetchone()
            if existing and existing["status"] != "failed":
                await conn.rollback()
                return False, f"This {kind} is already {existing['status']}."
            payload = json.dumps({"prompt": prompt, "title": title})
            if existing:
                await conn.execute(
                    """UPDATE ai_jobs SET status='running',input_json=?,destination_channel_id=?,
                       error_text=NULL,updated_at=? WHERE guild_id=? AND season=? AND source_key=?
                       AND kind=? AND prompt_version=? AND status='failed'""",
                    (payload, channel_id, now, guild_id, season, source_key, kind, PROMPT_VERSION),
                )
            else:
                await conn.execute(
                    """INSERT INTO ai_jobs
                       (guild_id,season,source_key,kind,prompt_version,status,input_json,
                        destination_channel_id,created_at,updated_at)
                       VALUES (?,?,?,?,?,'running',?,?,?,?)""",
                    (guild_id, season, source_key, kind, PROMPT_VERSION, payload, channel_id, now, now),
                )
            await conn.commit()
        try:
            text = await self.generate(guild_id, prompt)
            guild = self.client.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError("The configured destination channel is unavailable.")
            message = await channel.send(
                embed=discord.Embed(title=title[:256], description=text, color=discord.Color.purple()),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.db.execute(
                """UPDATE ai_jobs SET status='posted',output_text=?,posted_message_id=?,
                   attempts=attempts+1,updated_at=? WHERE guild_id=? AND season=?
                   AND source_key=? AND kind=? AND prompt_version=?""",
                (text, message.id, iso_now(), guild_id, season, source_key, kind, PROMPT_VERSION),
            )
            return True, "Posted."
        except Exception as exc:
            await self.db.execute(
                """UPDATE ai_jobs SET status='failed',attempts=attempts+1,error_text=?,updated_at=?
                   WHERE guild_id=? AND season=? AND source_key=? AND kind=? AND prompt_version=?""",
                (str(exc)[:500], iso_now(), guild_id, season, source_key, kind, PROMPT_VERSION),
            )
            log.warning("AI job %s/%s failed: %s", kind, source_key, exc)
            return False, str(exc)

    async def matchup_preview(self, matchup_id: int, *, force_key: str | None = None):
        matchup = await self.db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
        if not matchup or not matchup["channel_id"]:
            return False, "That matchup or its channel was not found."
        settings = await self.db.settings(matchup["guild_id"])
        prompt = (
            "Write a concise Madden league game preview under 250 words. Use only these facts; "
            "do not invent players, injuries, stats, quotes, or betting lines. "
            f"Tone: {settings.get('ai_style')}. League: {settings['league_name']}. "
            f"Season {matchup['season']}, Week {matchup['week']}: "
            f"{matchup['away_team']} at {matchup['home_team']}."
        )
        key = force_key or f"matchup:{matchup_id}"
        return await self.generate_and_post(
            guild_id=matchup["guild_id"], season=matchup["season"], source_key=key,
            kind="preview", prompt=prompt, channel_id=matchup["channel_id"],
            title=f"\N{CRYSTAL BALL} Week {matchup['week']} Game Preview",
        )

    async def matchup_recap(self, matchup_id: int, *, force_key: str | None = None):
        matchup = await self.db.fetchone("SELECT * FROM matchups WHERE id=?", (matchup_id,))
        if not matchup or not matchup["channel_id"] or matchup["away_score"] is None:
            return False, "That matchup has no scored final or matchup channel."
        settings = await self.db.settings(matchup["guild_id"])
        prompt = (
            "Write a concise Madden league game recap under 250 words. Use only the supplied score; "
            "do not invent players, drives, statistics, quotes, or events. "
            f"Tone: {settings.get('ai_style')}. Week {matchup['week']} final: "
            f"{matchup['away_team']} {matchup['away_score']}, "
            f"{matchup['home_team']} {matchup['home_score']}."
        )
        return await self.generate_and_post(
            guild_id=matchup["guild_id"], season=matchup["season"],
            source_key=force_key or f"matchup:{matchup_id}", kind="recap", prompt=prompt,
            channel_id=matchup["channel_id"], title="\N{NEWSPAPER} Official Game Recap",
        )

    async def weekly_content(
        self, guild_id: int, season: str, week: int, *, force_key: str | None = None
    ):
        from .weekly_content import publish_weekly_recap
        return await publish_weekly_recap(
            self.client, self.db, guild_id, season, week,
            create=True, regenerate_ai=True,
        )
    async def player_of_week(
        self, guild_id: int, season: str, week: int, player: str, team: str, stats: str,
        *, force_key: str | None = None,
    ):
        settings = await self.db.settings(guild_id)
        channel_id = settings.get("storyline_channel_id")
        if not channel_id:
            return False, "Configure `/setstorylinechannel` first."
        safe_player, safe_team, safe_stats = map(sanitize_ai_text, (player, team, stats))
        prompt = (
            "Write a celebratory Madden Player of the Week announcement under 180 words using only "
            f"these commissioner-supplied facts. Player: {safe_player}. Team: {safe_team}. "
            f"Stats: {safe_stats}. Do not add facts. Tone: {settings.get('ai_style')}."
        )
        return await self.generate_and_post(
            guild_id=guild_id, season=season,
            source_key=force_key or f"potw:{week}:{safe_player.casefold()}", kind="potw",
            prompt=prompt, channel_id=channel_id, title=f"\N{TROPHY} Week {week} Player of the Week",
        )

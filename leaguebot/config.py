from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    database_path: Path
    backup_dir: Path
    twitch_client_id: str | None
    twitch_client_secret: str | None
    youtube_api_key: str | None
    log_level: str
    stream_poll_seconds: int
    reminder_poll_seconds: int
    gemini_api_key: str | None
    gemini_model: str
    ai_enabled: bool
    ai_daily_limit: int

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Config":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_token and not token:
            raise RuntimeError("DISCORD_TOKEN is required. Copy .env.example to .env and add it.")
        return cls(
            token=token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/leaguebot.sqlite3")),
            backup_dir=Path(os.getenv("BACKUP_DIR", "data/backups")),
            twitch_client_id=os.getenv("TWITCH_CLIENT_ID") or None,
            twitch_client_secret=os.getenv("TWITCH_CLIENT_SECRET") or None,
            youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            stream_poll_seconds=max(60, int(os.getenv("STREAM_POLL_SECONDS", "180"))),
            reminder_poll_seconds=max(60, int(os.getenv("REMINDER_POLL_SECONDS", "300"))),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            ai_enabled=os.getenv("AI_ENABLED", "true").strip().casefold()
            in {"1", "true", "yes", "on"},
            ai_daily_limit=max(1, int(os.getenv("AI_DAILY_LIMIT", "100"))),
        )

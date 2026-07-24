from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FINAL_STATUSES = frozenset({"complete", "force_home", "force_away", "fair_sim"})
ALL_STATUSES = FINAL_STATUSES | frozenset(
    {"waiting", "schedule_pending", "scheduled", "result_pending", "issue_reported"}
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def next_deadline(
    timezone: str, advance_weekday: int, advance_time: str, *, now: datetime | None = None
) -> datetime:
    zone = ZoneInfo(timezone)
    local_now = (now or utcnow()).astimezone(zone)
    hour, minute = (int(part) for part in advance_time.split(":", 1))
    days = (advance_weekday - local_now.weekday()) % 7
    candidate = (local_now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def parse_user_datetime(text: str, timezone: str) -> datetime:
    cleaned = text.strip()
    formats = ("%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d %I:%M %p")
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if fmt == "%m/%d %I:%M %p":
                parsed = parsed.replace(year=datetime.now(ZoneInfo(timezone)).year)
            return parsed.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
        except ValueError:
            continue
    raise ValueError("Use YYYY-MM-DD HH:MM or MM/DD/YYYY HH:MM AM/PM.")


def stable_game_key(week: int, away: str, home: str, source_id: str = "") -> str:
    normalized = "|".join(
        [str(week), source_id.strip().lower(), away.strip().lower(), home.strip().lower()]
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def slugify(value: str, fallback: str = "league") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:90] or fallback


def status_label(status: str, deadline: str | None = None) -> str:
    if status == "waiting" and deadline:
        try:
            if datetime.fromisoformat(deadline) < utcnow():
                return "Overdue"
        except ValueError:
            pass
    return {
        "waiting": "Waiting",
        "schedule_pending": "Awaiting Schedule Confirmation",
        "scheduled": "Scheduled",
        "result_pending": "Review Result",
        "issue_reported": "Issue Reported",
        "complete": "Complete",
        "force_home": "Force Win (Home)",
        "force_away": "Force Win (Away)",
        "fair_sim": "Fair Sim",
    }.get(status, status.replace("_", " ").title())


from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .helpers import stable_game_key


@dataclass(frozen=True, slots=True)
class ImportGame:
    week: int
    away_team: str
    home_team: str
    away_user_id: int | None
    home_user_id: int | None
    external_key: str


ALIASES = {
    "week": {"week", "week number", "week_number", "wk"},
    "away": {"away", "away team", "away_team", "visitor", "visitor team"},
    "home": {"home", "home team", "home_team"},
    "away_user": {"away user id", "away_user_id", "away discord id"},
    "home_user": {"home user id", "home_user_id", "home discord id"},
    "source_id": {"game id", "game_id", "id", "external id"},
}


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def parse_schedule_csv(content: bytes | str) -> tuple[list[ImportGame], list[str]]:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    else:
        text = content.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The CSV has no header row."]
    field_map: dict[str, str] = {}
    normalized_fields = {_normalize(field): field for field in reader.fieldnames}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if _normalize(alias) in normalized_fields:
                field_map[canonical] = normalized_fields[_normalize(alias)]
                break
    missing = [name for name in ("week", "away", "home") if name not in field_map]
    if missing:
        return [], [f"Missing required column: {name}" for name in missing]

    games: list[ImportGame] = []
    errors: list[str] = []
    seen: set[tuple[int, str]] = set()
    for line, row in enumerate(reader, start=2):
        try:
            week = int((row.get(field_map["week"]) or "").strip())
            away = (row.get(field_map["away"]) or "").strip()
            home = (row.get(field_map["home"]) or "").strip()
            if week < 1 or not away or not home or away.casefold() == home.casefold():
                raise ValueError("week must be positive and teams must be different")
            source_id = (row.get(field_map.get("source_id", "")) or "").strip()
            key = stable_game_key(week, away, home, source_id)
            identity = (week, key)
            if identity in seen:
                raise ValueError("duplicate game in file")
            seen.add(identity)
            away_user = _optional_int(row.get(field_map.get("away_user", "")))
            home_user = _optional_int(row.get(field_map.get("home_user", "")))
            games.append(ImportGame(week, away, home, away_user, home_user, key))
        except (ValueError, TypeError) as exc:
            errors.append(f"Row {line}: {exc}.")
    return games, errors


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value.strip())
    if parsed <= 0:
        raise ValueError("Discord user IDs must be positive")
    return parsed


def college_template() -> str:
    return (
        "week,away_team,home_team,away_user_id,home_user_id,game_id\n"
        "1,Example Away,Example Home,,,week1-game1\n"
    )

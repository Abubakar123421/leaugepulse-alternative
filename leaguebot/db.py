from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from .helpers import iso_now

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    league_name TEXT NOT NULL DEFAULT 'My League',
    game TEXT NOT NULL DEFAULT 'Madden 26',
    season TEXT NOT NULL DEFAULT '1',
    current_week INTEGER NOT NULL DEFAULT 1,
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    advance_weekday INTEGER NOT NULL DEFAULT 2 CHECK(advance_weekday BETWEEN 0 AND 6),
    advance_time TEXT NOT NULL DEFAULT '21:00',
    category_id INTEGER,
    matchup_category_id INTEGER,
    matchups_channel_id INTEGER,
    announcements_channel_id INTEGER,
    final_scores_channel_id INTEGER,
    storyline_channel_id INTEGER,
    trade_channel_id INTEGER,
    open_teams_channel_id INTEGER,
    polls_channel_id INTEGER,
    recruiting_channel_id INTEGER,
    transactions_channel_id INTEGER,
    streams_channel_id INTEGER,
    audit_channel_id INTEGER,
    open_teams_message_id INTEGER,
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    ai_style TEXT NOT NULL DEFAULT 'professional sports broadcast',
    season_started_at TEXT,
    week_started_at TEXT,
    week_deadline_at TEXT,
    auto_week_rollover INTEGER NOT NULL DEFAULT 1,
    regular_season_weeks INTEGER NOT NULL DEFAULT 18,
    commissioner_role_id INTEGER,
    reminder_24h INTEGER NOT NULL DEFAULT 1,
    reminder_48 INTEGER NOT NULL DEFAULT 1,
    reminder_24 INTEGER NOT NULL DEFAULT 1,
    reminder_6 INTEGER NOT NULL DEFAULT 1,
    features TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    name TEXT NOT NULL,
    logo_url TEXT,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    ties INTEGER NOT NULL DEFAULT 0,
    UNIQUE(guild_id, season, name)
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    twitch TEXT,
    youtube TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_by INTEGER,
    assignment_source TEXT NOT NULL DEFAULT 'commissioner',
    external_team_id TEXT,
    assigned_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    external_key TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_user_id INTEGER,
    home_user_id INTEGER,
    away_score INTEGER,
    home_score INTEGER,
    proposed_by INTEGER,
    proposed_at TEXT,
    scheduled_at TEXT,
    schedule_previous_at TEXT,
    schedule_proposal_version INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    issue_text TEXT,
    thread_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    commissioner_pinged_at TEXT,
    result_submission_version INTEGER NOT NULL DEFAULT 0,
    result_submitted_by INTEGER,
    result_submitted_at TEXT,
    result_evidence_url TEXT,
    result_opponent_status TEXT,
    result_opponent_by INTEGER,
    result_audit_message_id INTEGER,
    result_reviewed_by INTEGER,
    result_reviewed_at TEXT,
    result_review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(guild_id, season, week, external_key)
);

CREATE TABLE IF NOT EXISTS career_profiles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    force_wins INTEGER NOT NULL DEFAULT 0,
    forfeits INTEGER NOT NULL DEFAULT 0,
    sims INTEGER NOT NULL DEFAULT 0,
    points_for INTEGER NOT NULL DEFAULT 0,
    points_against INTEGER NOT NULL DEFAULT 0,
    xp INTEGER NOT NULL DEFAULT 0,
    championships INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS season_participants (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    force_wins INTEGER NOT NULL DEFAULT 0,
    forfeits INTEGER NOT NULL DEFAULT 0,
    sims INTEGER NOT NULL DEFAULT 0,
    points_for INTEGER NOT NULL DEFAULT 0,
    points_against INTEGER NOT NULL DEFAULT 0,
    xp INTEGER NOT NULL DEFAULT 0,
    champion INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, user_id, team_name)
);

CREATE TABLE IF NOT EXISTS career_events (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    source_key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, source_key, user_id, event_type)
);

CREATE TABLE IF NOT EXISTS season_archives (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    league_name TEXT NOT NULL,
    game TEXT NOT NULL,
    champion_user_id INTEGER,
    champion_team TEXT,
    total_games INTEGER NOT NULL DEFAULT 0,
    cleanup_status TEXT NOT NULL DEFAULT 'pending',
    db_compacted INTEGER NOT NULL DEFAULT 0,
    old_category_id INTEGER,
    cleanup_error TEXT,
    archived_by INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season)
);

CREATE TABLE IF NOT EXISTS game_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    external_key TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_user_id INTEGER,
    home_user_id INTEGER,
    away_score INTEGER,
    home_score INTEGER,
    status TEXT NOT NULL,
    winner_user_id INTEGER,
    decision_type TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE(guild_id, season, week, external_key)
);

CREATE TABLE IF NOT EXISTS matchup_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchup_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    opened_by INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_deadline_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    resolved_by INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(matchup_id) REFERENCES matchups(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS team_roles (
    guild_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, team_name),
    UNIQUE(guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS week_categories (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, week)
);

CREATE TABLE IF NOT EXISTS matchup_prompts (
    matchup_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(matchup_id, user_id),
    FOREIGN KEY(matchup_id) REFERENCES matchups(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    matchup_id INTEGER NOT NULL,
    milestone TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(matchup_id, milestone),
    FOREIGN KEY(matchup_id) REFERENCES matchups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    kind TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_block (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_rosters (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    team_name TEXT NOT NULL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, team_name)
);

CREATE TABLE IF NOT EXISTS open_team_cards (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    external_team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, external_team_id),
    UNIQUE(guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS stream_alert_state (
    guild_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    channel_key TEXT NOT NULL,
    live_id TEXT,
    last_live_at TEXT,
    PRIMARY KEY(guild_id, platform, channel_key)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);



CREATE TABLE IF NOT EXISTS franchises (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    external_team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    abbreviation TEXT,
    emoji_name TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, external_team_id),
    UNIQUE(guild_id, season, team_name)
);

CREATE TABLE IF NOT EXISTS roster_players (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    external_player_id TEXT NOT NULL,
    external_team_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    position TEXT NOT NULL,
    jersey_number TEXT,
    overall INTEGER,
    scheme_overall INTEGER,
    dev_trait TEXT,
    age INTEGER,
    height TEXT,
    weight INTEGER,
    years_pro INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_on_ir INTEGER NOT NULL DEFAULT 0,
    is_practice_squad INTEGER NOT NULL DEFAULT 0,
    injury_type TEXT,
    injury_length INTEGER,
    contract_years_left INTEGER,
    contract_salary INTEGER,
    cap_hit INTEGER,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, external_player_id)
);

CREATE TABLE IF NOT EXISTS week_rollovers (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    from_week INTEGER NOT NULL,
    to_week INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    unresolved_count INTEGER NOT NULL DEFAULT 0,
    error_text TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(guild_id, season, from_week)
);
CREATE TABLE IF NOT EXISTS ai_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    source_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_text TEXT,
    destination_channel_id INTEGER,
    posted_message_id INTEGER,
    error_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(guild_id, season, source_key, kind, prompt_version)
);

CREATE TABLE IF NOT EXISTS ai_daily_usage (
    usage_date TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(usage_date, guild_id)
);
CREATE TABLE IF NOT EXISTS weekly_mvps (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    stats_text TEXT NOT NULL,
    entered_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, week)
);
CREATE TABLE IF NOT EXISTS weekly_recaps (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    facts_json TEXT NOT NULL DEFAULT '{}',
    narrative_text TEXT,
    status TEXT NOT NULL DEFAULT 'partial',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, week)
);
CREATE TABLE IF NOT EXISTS game_of_week_posts (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    matchup_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, week)
);
CREATE TABLE IF NOT EXISTS season_awards (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    category TEXT NOT NULL,
    recipient TEXT NOT NULL,
    details TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'commissioner',
    entered_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season, category)
);
CREATE TABLE IF NOT EXISTS season_award_summaries (
    guild_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    approved_by INTEGER,
    approved_at TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, season)
);
CREATE INDEX IF NOT EXISTS idx_matchups_due ON matchups(status, deadline_at);
CREATE INDEX IF NOT EXISTS idx_matchups_guild_week ON matchups(guild_id, season, week);
CREATE INDEX IF NOT EXISTS idx_profiles_team ON profiles(guild_id, team_name, approved);
CREATE INDEX IF NOT EXISTS idx_cases_matchup_status ON matchup_cases(matchup_id, status);
CREATE INDEX IF NOT EXISTS idx_history_guild_season ON game_history(guild_id, season, week);
CREATE INDEX IF NOT EXISTS idx_participants_user ON season_participants(guild_id, user_id, season);
CREATE INDEX IF NOT EXISTS idx_career_rank ON career_profiles(guild_id, xp DESC);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_pending ON ai_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_roster_team ON roster_players(guild_id, season, external_team_id);
CREATE INDEX IF NOT EXISTS idx_roster_name ON roster_players(guild_id, season, full_name);
"""

MATCHUP_RESULT_COLUMNS = {
    "channel_id": "INTEGER",
    "schedule_previous_at": "TEXT",
    "schedule_proposal_version": "INTEGER NOT NULL DEFAULT 0",
    "result_submission_version": "INTEGER NOT NULL DEFAULT 0",
    "result_submitted_by": "INTEGER",
    "result_submitted_at": "TEXT",
    "result_evidence_url": "TEXT",
    "result_opponent_status": "TEXT",
    "result_opponent_by": "INTEGER",
    "result_audit_message_id": "INTEGER",
    "result_reviewed_by": "INTEGER",
    "result_reviewed_at": "TEXT",
    "result_review_note": "TEXT",
}

TABLE_MIGRATIONS = {
    "guild_settings": {
        "matchup_category_id": "INTEGER",
        "final_scores_channel_id": "INTEGER",
        "storyline_channel_id": "INTEGER",
        "trade_channel_id": "INTEGER",
        "open_teams_channel_id": "INTEGER",
        "polls_channel_id": "INTEGER",
        "recruiting_channel_id": "INTEGER",
        "open_teams_message_id": "INTEGER",
        "ai_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_style": "TEXT NOT NULL DEFAULT 'professional sports broadcast'",
        "season_started_at": "TEXT",
        "week_started_at": "TEXT",
        "week_deadline_at": "TEXT",
        "auto_week_rollover": "INTEGER NOT NULL DEFAULT 1",
        "regular_season_weeks": "INTEGER NOT NULL DEFAULT 18",
    },
    "profiles": {
        "assignment_source": "TEXT NOT NULL DEFAULT 'commissioner'",
        "assigned_at": "TEXT",
        "external_team_id": "TEXT",
    },
    "matchups": {
        **MATCHUP_RESULT_COLUMNS,
        "away_team_id": "TEXT",
        "home_team_id": "TEXT",
        "final_score_message_id": "INTEGER",
        "final_score_posted_at": "TEXT",
    },
    "franchises": {
        "emoji_name": "TEXT",
    },
    "season_archives": {
        "db_compacted": "INTEGER NOT NULL DEFAULT 0",
        "old_category_id": "INTEGER",
        "cleanup_error": "TEXT",
    },
}

DEFAULT_FEATURES = {
    "trades": True,
    "transfers": True,
    "open_rosters": True,
    "announcements": True,
    "awards": True,
    "streams": True,
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.executescript(SCHEMA)
            for table, migrations in TABLE_MIGRATIONS.items():
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                existing_columns = {row[1] for row in await cursor.fetchall()}
                for name, declaration in migrations.items():
                    if name not in existing_columns:
                        await conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            await conn.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self.connect() as conn:
            cursor = await conn.execute(sql, tuple(params))
            await conn.commit()
            return cursor.lastrowid

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.connect() as conn:
            cursor = await conn.execute(sql, tuple(params))
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.connect() as conn:
            cursor = await conn.execute(sql, tuple(params))
            return list(await cursor.fetchall())

    async def ensure_guild(self, guild_id: int) -> aiosqlite.Row:
        now = iso_now()
        await self.execute(
            """INSERT INTO guild_settings
               (guild_id, features, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO NOTHING""",
            (guild_id, json.dumps(DEFAULT_FEATURES), now, now),
        )
        result = await self.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        assert result is not None
        return result

    async def settings(self, guild_id: int) -> dict[str, Any]:
        row = await self.ensure_guild(guild_id)
        value = dict(row)
        value["features"] = {**DEFAULT_FEATURES, **json.loads(value["features"] or "{}")}
        return value

    async def update_settings(self, guild_id: int, **values: Any) -> None:
        allowed = {
            "league_name", "game", "season", "current_week", "timezone",
            "advance_weekday", "advance_time", "category_id", "matchups_channel_id",
            "matchup_category_id", "announcements_channel_id",
            "final_scores_channel_id", "storyline_channel_id", "trade_channel_id",
            "open_teams_channel_id", "polls_channel_id", "recruiting_channel_id",
            "transactions_channel_id", "streams_channel_id", "audit_channel_id",
            "commissioner_role_id", "reminder_24h",
            "reminder_48", "reminder_24", "reminder_6", "features",
            "open_teams_message_id", "ai_enabled", "ai_style",
            "season_started_at", "week_started_at", "week_deadline_at",
            "auto_week_rollover", "regular_season_weeks",
        }
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")
        await self.ensure_guild(guild_id)
        if "features" in values and not isinstance(values["features"], str):
            values["features"] = json.dumps(values["features"])
        values["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        await self.execute(
            f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
            (*values.values(), guild_id),
        )

    async def audit(
        self, guild_id: int, actor_id: int, action: str, *,
        target_type: str | None = None, target_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        return await self.execute(
            """INSERT INTO audit_logs
               (guild_id, actor_id, action, target_type, target_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id, actor_id, action, target_type, target_id,
                json.dumps(details or {}, sort_keys=True), iso_now(),
            ),
        )

    async def compact(self) -> bool:
        """Best-effort SQLite compaction after operational season data is purged."""
        try:
            async with aiosqlite.connect(self.path) as conn:
                await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await conn.execute("VACUUM")
            return True
        except aiosqlite.OperationalError:
            return False
    async def feature_enabled(self, guild_id: int, feature: str) -> bool:
        return bool((await self.settings(guild_id))["features"].get(feature, False))


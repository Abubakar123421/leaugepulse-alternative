from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import FINAL_STATUSES, iso_now, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FixtureImportRow:
    fixture_id: str
    week: int
    away_team_id: str
    away_abbr: str
    away_team: str
    home_team_id: str
    home_abbr: str
    home_team: str


@dataclass(frozen=True, slots=True)
class RosterImportRow:
    team_id: str
    team_abbr: str
    team_name: str
    player_id: str
    player_name: str
    position: str
    values: dict[str, str]


def _reader(content: bytes | str):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
    return csv.DictReader(io.StringIO(text))


def parse_fixture_import(content: bytes | str) -> tuple[list[FixtureImportRow], list[str]]:
    reader = _reader(content)
    required = {
        "fixture_id", "week", "away_team_id", "away_abbr", "away_team",
        "home_team_id", "home_abbr", "home_team",
    }
    fields = set(reader.fieldnames or ())
    if missing := sorted(required - fields):
        return [], [f"Missing required column: {name}" for name in missing]
    rows: list[FixtureImportRow] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_games: set[tuple[int, str, str]] = set()
    team_identity: dict[str, tuple[str, str]] = {}
    for line, raw in enumerate(reader, 2):
        try:
            fixture_id = raw["fixture_id"].strip()
            week = int(raw["week"])
            away_id, home_id = raw["away_team_id"].strip(), raw["home_team_id"].strip()
            away, home = raw["away_team"].strip(), raw["home_team"].strip()
            away_abbr, home_abbr = raw["away_abbr"].strip(), raw["home_abbr"].strip()
            if not fixture_id or not away_id or not home_id or not away or not home:
                raise ValueError("fixture and team IDs/names are required")
            if not 1 <= week <= 99 or away_id == home_id or away.casefold() == home.casefold():
                raise ValueError("invalid week or identical teams")
            if fixture_id in seen_ids:
                raise ValueError("duplicate fixture_id")
            identity = (week, away_id, home_id)
            if identity in seen_games:
                raise ValueError("duplicate week matchup")
            for team_id, name, abbr in (
                (away_id, away, away_abbr), (home_id, home, home_abbr)
            ):
                existing = team_identity.setdefault(team_id, (name.casefold(), abbr.casefold()))
                if existing != (name.casefold(), abbr.casefold()):
                    raise ValueError(f"team ID {team_id} has conflicting names")
            seen_ids.add(fixture_id)
            seen_games.add(identity)
            rows.append(FixtureImportRow(
                fixture_id, week, away_id, away_abbr, away,
                home_id, home_abbr, home,
            ))
        except (ValueError, TypeError) as exc:
            errors.append(f"Row {line}: {exc}.")
    if not rows:
        errors.append("The fixture file contains no games.")
    return rows, errors


def parse_roster_import(content: bytes | str) -> tuple[list[RosterImportRow], list[str]]:
    reader = _reader(content)
    required = {"team_id", "team_abbr", "team_name", "player_id", "player_name", "position"}
    fields = set(reader.fieldnames or ())
    if missing := sorted(required - fields):
        return [], [f"Missing required column: {name}" for name in missing]
    rows: list[RosterImportRow] = []
    errors: list[str] = []
    seen_players: set[str] = set()
    team_identity: dict[str, tuple[str, str]] = {}
    for line, raw in enumerate(reader, 2):
        try:
            values = {key: (value or "").strip() for key, value in raw.items() if key}
            team_id, name = values["team_id"], values["team_name"]
            abbr, player_id = values["team_abbr"], values["player_id"]
            player_name, position = values["player_name"], values["position"]
            if not all((team_id, name, player_id, player_name, position)):
                raise ValueError("team, player ID, name, and position are required")
            if player_id in seen_players:
                raise ValueError("duplicate player_id")
            existing = team_identity.setdefault(team_id, (name.casefold(), abbr.casefold()))
            if existing != (name.casefold(), abbr.casefold()):
                raise ValueError(f"team ID {team_id} has conflicting names")
            seen_players.add(player_id)
            rows.append(RosterImportRow(team_id, abbr, name, player_id, player_name, position, values))
        except (ValueError, TypeError) as exc:
            errors.append(f"Row {line}: {exc}.")
    if not rows:
        errors.append("The roster file contains no assigned players.")
    return rows, errors


def roster_team_summary(rows: list[RosterImportRow]) -> list[tuple[str, str, str, int]]:
    teams: dict[str, tuple[str, str, int]] = {}
    for row in rows:
        current = teams.get(row.team_id, (row.team_name, row.team_abbr, 0))
        teams[row.team_id] = (current[0], current[1], current[2] + 1)
    return sorted(
        ((team_id, name, abbr, count) for team_id, (name, abbr, count) in teams.items()),
        key=lambda item: item[1].casefold(),
    )


def _integer(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _boolean(value: str | None) -> int:
    return int(str(value).strip().casefold() in {"1", "true", "yes"})


async def apply_roster_import(
    db: Database, guild_id: int, season: str, rows: list[RosterImportRow]
) -> tuple[int, int]:
    now = iso_now()
    teams = roster_team_summary(rows)
    team_ids = {item[0] for item in teams}
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "DELETE FROM roster_players WHERE guild_id=? AND season=?", (guild_id, season)
        )
        for sort_order, (team_id, name, abbr, _) in enumerate(teams):
            await conn.execute(
                """INSERT INTO franchises
                   (guild_id,season,external_team_id,team_name,abbreviation,sort_order,imported_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(guild_id,season,external_team_id)
                   DO UPDATE SET team_name=excluded.team_name,abbreviation=excluded.abbreviation,
                   sort_order=excluded.sort_order,imported_at=excluded.imported_at""",
                (guild_id, season, team_id, name, abbr, sort_order, now),
            )
            await conn.execute(
                """INSERT INTO teams (guild_id,season,name) VALUES (?,?,?)
                   ON CONFLICT(guild_id,season,name) DO NOTHING""",
                (guild_id, season, name),
            )
        placeholders = ",".join("?" for _ in team_ids)
        if team_ids:
            await conn.execute(
                f"DELETE FROM franchises WHERE guild_id=? AND season=? AND external_team_id NOT IN ({placeholders})",
                (guild_id, season, *team_ids),
            )
        if teams:
            name_placeholders = ",".join("?" for _ in teams)
            await conn.execute(
                f"""DELETE FROM open_rosters WHERE guild_id=? AND season=?
                    AND lower(team_name) NOT IN ({name_placeholders})""",
                (guild_id, season, *(name.casefold() for _, name, _, _ in teams)),
            )
        for row in rows:
            v = row.values
            common = {
                "team_id", "team_abbr", "team_name", "player_id", "player_name", "position",
                "jersey_number", "overall", "scheme_overall", "dev_trait", "age", "height",
                "weight", "years_pro", "is_active", "is_on_ir", "is_practice_squad",
                "injury_type", "injury_length", "contract_years_left", "contract_salary", "cap_hit",
            }
            attributes = {key: value for key, value in v.items() if key not in common and value != ""}
            await conn.execute(
                """INSERT INTO roster_players
                   (guild_id,season,external_player_id,external_team_id,team_name,full_name,
                    position,jersey_number,overall,scheme_overall,dev_trait,age,height,weight,
                    years_pro,is_active,is_on_ir,is_practice_squad,injury_type,injury_length,
                    contract_years_left,contract_salary,cap_hit,attributes_json,imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    guild_id, season, row.player_id, row.team_id, row.team_name, row.player_name,
                    row.position, v.get("jersey_number"), _integer(v.get("overall")),
                    _integer(v.get("scheme_overall")), v.get("dev_trait"), _integer(v.get("age")),
                    v.get("height"), _integer(v.get("weight")), _integer(v.get("years_pro")),
                    _boolean(v.get("is_active")), _boolean(v.get("is_on_ir")),
                    _boolean(v.get("is_practice_squad")), v.get("injury_type"),
                    _integer(v.get("injury_length")), _integer(v.get("contract_years_left")),
                    _integer(v.get("contract_salary")), _integer(v.get("cap_hit")),
                    json.dumps(attributes, sort_keys=True), now,
                ),
            )
        # Keep season ownership stable by linking existing name-based claims to imported IDs.
        for team_id, name, _, _ in teams:
            await conn.execute(
                "UPDATE profiles SET external_team_id=?,team_name=?,updated_at=? WHERE guild_id=? AND lower(team_name)=lower(?)",
                (team_id, name, now, guild_id, name),
            )
        await conn.commit()
    return len(teams), len(rows)


async def apply_fixture_import(
    db: Database, guild_id: int, season: str, rows: list[FixtureImportRow], *, start_now: bool
) -> tuple[int, int, int]:
    franchises = {
        row["external_team_id"]: row
        for row in await db.fetchall(
            "SELECT * FROM franchises WHERE guild_id=? AND season=?", (guild_id, season)
        )
    }
    fixture_team_ids = {
        value for row in rows for value in (row.away_team_id, row.home_team_id)
    }
    missing = fixture_team_ids - franchises.keys()
    if missing:
        raise ValueError(
            f"{len(missing)} fixture team ID(s) are not in the roster import. Import rosters first."
        )
    now_dt = utcnow()
    now = now_dt.isoformat()
    created = updated = 0
    max_week = max(row.week for row in rows)
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            cursor = await conn.execute(
                "SELECT * FROM matchups WHERE guild_id=? AND season=? AND external_key=?",
                (guild_id, season, row.fixture_id),
            )
            existing = await cursor.fetchone()
            if existing and existing["status"] in FINAL_STATUSES:
                if (
                    existing["away_team_id"] != row.away_team_id
                    or existing["home_team_id"] != row.home_team_id
                    or existing["week"] != row.week
                ):
                    await conn.rollback()
                    raise ValueError(f"Fixture {row.fixture_id} conflicts with an official result.")
                continue
            deadline = (now_dt + timedelta(days=row.week * 7)).isoformat()
            away_owner_cursor = await conn.execute(
                "SELECT user_id FROM profiles WHERE guild_id=? AND approved=1 AND external_team_id=?",
                (guild_id, row.away_team_id),
            )
            away_owner = await away_owner_cursor.fetchone()
            home_owner_cursor = await conn.execute(
                "SELECT user_id FROM profiles WHERE guild_id=? AND approved=1 AND external_team_id=?",
                (guild_id, row.home_team_id),
            )
            home_owner = await home_owner_cursor.fetchone()
            if existing:
                await conn.execute(
                    """UPDATE matchups SET week=?,away_team=?,home_team=?,away_team_id=?,home_team_id=?,
                       away_user_id=?,home_user_id=?,deadline_at=?,updated_at=? WHERE id=?""",
                    (
                        row.week, row.away_team, row.home_team, row.away_team_id, row.home_team_id,
                        away_owner["user_id"] if away_owner else None,
                        home_owner["user_id"] if home_owner else None,
                        deadline, now, existing["id"],
                    ),
                )
                updated += 1
            else:
                await conn.execute(
                    """INSERT INTO matchups
                       (guild_id,season,week,external_key,away_team,home_team,away_team_id,
                        home_team_id,away_user_id,home_user_id,deadline_at,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        guild_id, season, row.week, row.fixture_id, row.away_team, row.home_team,
                        row.away_team_id, row.home_team_id,
                        away_owner["user_id"] if away_owner else None,
                        home_owner["user_id"] if home_owner else None,
                        deadline, "waiting", now, now,
                    ),
                )
                created += 1
        if start_now:
            await conn.execute(
                """UPDATE guild_settings SET current_week=1,season_started_at=?,week_started_at=?,
                   week_deadline_at=?,auto_week_rollover=1,regular_season_weeks=?,updated_at=?
                   WHERE guild_id=?""",
                (
                    now, now, (now_dt + timedelta(days=7)).isoformat(), max_week,
                    now, guild_id,
                ),
            )
        else:
            await conn.execute(
                "UPDATE guild_settings SET regular_season_weeks=?,updated_at=? WHERE guild_id=?",
                (max_week, now, guild_id),
            )
        await conn.commit()
    return created, updated, max_week


class ConfirmRosterImportView(discord.ui.View):
    def __init__(self, db: Database, rows: list[RosterImportRow], author_id: int):
        super().__init__(timeout=600)
        self.db, self.rows, self.author_id = db, rows, author_id
        self._background_tasks: set[asyncio.Task] = set()

    @discord.ui.button(label="Confirm Roster Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This import belongs to another Commissioner.", ephemeral=True)
            return
        settings = await self.db.settings(interaction.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message("Only a Commissioner can confirm this import.", ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(
            content=(
                f"Roster import accepted. Saving **{len(self.rows)} players** now; "
                "the 32 roster cards will refresh in the background."
            ),
            view=self,
        )
        try:
            teams, players = await apply_roster_import(
                self.db, interaction.guild_id, settings["season"], self.rows
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Roster import failed: {exc}"[:1900], ephemeral=True
            )
            return
        from .team_roles import ensure_team_roles
        from .ownership import initialize_open_teams
        from .open_teams_ui import refresh_open_teams_panel
        created_roles, role_errors = await ensure_team_roles(
            interaction.guild, self.db, settings["season"]
        )
        await initialize_open_teams(self.db, interaction.guild_id, settings["season"])
        await self.db.audit(
            interaction.guild_id, interaction.user.id, "rosters_imported",
            details={"teams": teams, "players": players, "role_errors": role_errors},
        )
        await interaction.followup.send(
            f"Imported **{teams} teams** and **{players} roster players**. "
            f"Created **{created_roles}** missing team roles. "
            "The Open Teams roster cards are refreshing in the background."
            + (f" Role warnings: {'; '.join(role_errors[:5])}" if role_errors else ""),
            ephemeral=True,
        )

        async def refresh_cards() -> None:
            try:
                await refresh_open_teams_panel(
                    interaction.client, self.db, interaction.guild_id
                )
            except Exception:
                log.exception(
                    "Open Teams card refresh failed after roster import for guild %s",
                    interaction.guild_id,
                )

        task = asyncio.create_task(
            refresh_cards(),
            name=f"roster-card-refresh:{interaction.guild_id}:{settings['season']}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

class ConfirmFixtureImportView(discord.ui.View):
    def __init__(
        self, db: Database, rows: list[FixtureImportRow], author_id: int, start_now: bool
    ):
        super().__init__(timeout=600)
        self.db, self.rows, self.author_id, self.start_now = db, rows, author_id, start_now

    @discord.ui.button(label="Confirm Fixture Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This import belongs to another Commissioner.", ephemeral=True)
            return
        settings = await self.db.settings(interaction.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message("Only a Commissioner can confirm this import.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            created, updated, weeks = await apply_fixture_import(
                self.db, interaction.guild_id, settings["season"], self.rows,
                start_now=self.start_now,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        channels = 0
        errors: list[str] = []
        if self.start_now:
            from .channel_workflow import create_week_matchup_channels
            channels, errors = await create_week_matchup_channels(
                interaction, self.db, season=settings["season"], week=1
            )
        await self.db.audit(
            interaction.guild_id, interaction.user.id, "fixtures_imported",
            details={"created": created, "updated": updated, "weeks": weeks, "channels": channels},
        )
        button.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.followup.send(
            f"Stored **{created + updated} fixtures** across **{weeks} weeks**. "
            + (f"Week 1 started with **{channels} matchup channels**." if self.start_now else "The season clock was not changed.")
            + (f" Channel warnings: {'; '.join(errors[:5])}" if errors else ""),
            ephemeral=True,
        )

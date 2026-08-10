from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import iso_now
from .ownership import Assignment, canonical_team, sync_assignment_discord
from .progression import ensure_participant
from .team_roles import MADDEN_TEAMS, active_team_names, remove_team_role


log = logging.getLogger(__name__)

MEMBER_CSV_FORMAT = (
    "Required headers: `team` and either `discord_id` or `discord_username`. "
    "Optional headers: `twitch`, `youtube`.\n"
    "Recommended example:\n"
    "```csv\nteam,discord_id,twitch,youtube\n49ers,123456789012345678,,\n```"
)


@dataclass(frozen=True, slots=True)
class MemberImportRow:
    line: int
    user_id: int
    username: str
    team_name: str
    twitch: str | None = None
    youtube: str | None = None


@dataclass(frozen=True, slots=True)
class MemberImportPreview:
    rows: tuple[MemberImportRow, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


ALIASES = {
    "team": ("team", "team_name", "franchise"),
    "discord_id": ("discord_id", "user_id", "member_id", "id"),
    "discord_username": ("discord_username", "username", "discord", "member"),
    "twitch": ("twitch", "twitch_url"),
    "youtube": ("youtube", "youtube_url"),
}


def _field(row: dict[str, str], name: str) -> str:
    for alias in ALIASES[name]:
        value = row.get(alias, "").strip()
        if value:
            return value
    return ""


def parse_member_csv(
    data: bytes, guild: discord.Guild, valid_teams: tuple[str, ...] | None = None
) -> MemberImportPreview:
    errors: list[str] = []
    warnings: list[str] = []
    team_lookup = {team.casefold(): team for team in (valid_teams or MADDEN_TEAMS)}
    rows: list[MemberImportRow] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return MemberImportPreview((), ("The CSV must use UTF-8 encoding.",), ())
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return MemberImportPreview(
            (), ("The CSV has no header row. " + MEMBER_CSV_FORMAT,), ()
        )
    reader.fieldnames = [str(name).strip().casefold() for name in reader.fieldnames]
    headers = set(reader.fieldnames)
    if not headers.intersection(ALIASES["team"]):
        errors.append("Missing a team header (`team`, `team_name`, or `franchise`).")
    if not (
        headers.intersection(ALIASES["discord_id"])
        or headers.intersection(ALIASES["discord_username"])
    ):
        errors.append(
            "Missing a member header (`discord_id` is recommended; "
            "`discord_username` is also supported)."
        )
    if errors:
        errors.append(MEMBER_CSV_FORMAT)
        return MemberImportPreview((), tuple(errors), ())
    seen_users: dict[int, int] = {}
    seen_teams: dict[str, int] = {}
    for line, raw in enumerate(reader, start=2):
        row = {str(k).strip().casefold(): (v or "").strip() for k, v in raw.items() if k is not None}
        requested_team = _field(row, "team")
        team = team_lookup.get(requested_team.strip().casefold())
        if not requested_team:
            errors.append(f"Line {line}: team is required.")
            continue
        if not team:
            errors.append(f"Line {line}: '{requested_team}' is not a valid Madden team.")
            continue
        id_text = _field(row, "discord_id")
        username = _field(row, "discord_username")
        member = None
        if id_text:
            cleaned = id_text.strip("<@!>")
            if not cleaned.isdigit() or not (member := guild.get_member(int(cleaned))):
                errors.append(f"Line {line}: Discord ID '{id_text}' is not a current server member.")
                continue
        elif username:
            query = username.removeprefix("@").casefold()
            matches = [
                item for item in guild.members
                if query in {
                    item.name.casefold(), item.display_name.casefold(),
                    (item.global_name or "").casefold(), str(item).casefold(),
                }
            ]
            if len(matches) != 1:
                detail = "not found" if not matches else "ambiguous; use discord_id"
                errors.append(f"Line {line}: username '{username}' is {detail}.")
                continue
            member = matches[0]
        else:
            errors.append(f"Line {line}: discord_id or discord_username is required.")
            continue
        team_key = team.casefold()
        if member.id in seen_users:
            errors.append(f"Line {line}: member is duplicated (first seen on line {seen_users[member.id]}).")
            continue
        if team_key in seen_teams:
            errors.append(f"Line {line}: {team} is duplicated (first seen on line {seen_teams[team_key]}).")
            continue
        seen_users[member.id] = line
        seen_teams[team_key] = line
        rows.append(MemberImportRow(
            line, member.id, str(member), team, _field(row, "twitch") or None,
            _field(row, "youtube") or None,
        ))
    if not rows and not errors:
        errors.append("The CSV contains no member assignments.")
    return MemberImportPreview(tuple(rows), tuple(errors), tuple(warnings))


async def validate_import_conflicts(
    db: Database, guild_id: int, season: str, preview: MemberImportPreview, mode: str
) -> MemberImportPreview:
    errors = list(preview.errors)
    warnings = list(preview.warnings)
    if mode == "replace":
        incoming = {row.user_id for row in preview.rows}
        active = await db.fetchall("SELECT user_id,team_name FROM profiles WHERE guild_id=?", (guild_id,))
        omitted = [row for row in active if row["user_id"] not in incoming]
        for row in omitted:
            conflict = await db.fetchone(
                """SELECT id FROM matchups WHERE guild_id=? AND season=?
                   AND status IN ('result_pending','issue_reported') AND result_submitted_by IS NOT NULL
                   AND (away_user_id=? OR home_user_id=?) LIMIT 1""",
                (guild_id, season, row["user_id"], row["user_id"]),
            )
            if conflict:
                errors.append(
                    f"Cannot replace {row['team_name']}: its owner has matchup #{conflict['id']} under result review."
                )
        if omitted:
            warnings.append(f"Replace mode will release {len(omitted)} omitted current owner(s).")
    if mode == "merge":
        active = await db.fetchall(
            "SELECT user_id,team_name FROM profiles WHERE guild_id=?", (guild_id,)
        )
        by_user = {row["user_id"]: row["team_name"] for row in active}
        by_team = {row["team_name"].casefold(): row["user_id"] for row in active}
        for row in preview.rows:
            existing_owner = by_team.get(row.team_name.casefold())
            if existing_owner is not None and existing_owner != row.user_id:
                errors.append(
                    f"{row.team_name} is already assigned to <@{existing_owner}>; merge mode cannot release omitted owners."
                )
            existing_team = by_user.get(row.user_id)
            if existing_team and existing_team.casefold() != row.team_name.casefold():
                warnings.append(
                    f"<@{row.user_id}> will move from {existing_team} to {row.team_name}."
                )
    assigned = {row.team_name.casefold() for row in preview.rows}
    team_total = await db.fetchone(
        "SELECT COUNT(*) AS total FROM franchises WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    total = team_total["total"] if team_total and team_total["total"] else 32
    warnings.append(f"{max(0, total - len(assigned))} team(s) will remain open or unchanged according to import mode.")
    return MemberImportPreview(preview.rows, tuple(errors), tuple(warnings))


async def apply_member_import(
    db: Database, guild_id: int, season: str, rows: tuple[MemberImportRow, ...], mode: str
) -> list[tuple[int, str]]:
    now = iso_now()
    incoming_ids = {row.user_id for row in rows}
    released: list[tuple[int, str]] = []
    franchise_rows = await db.fetchall(
        "SELECT external_team_id,team_name FROM franchises WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    franchise_by_name = {
        row["team_name"].casefold(): row["external_team_id"] for row in franchise_rows
    }
    team_names = tuple(row["team_name"] for row in franchise_rows) or MADDEN_TEAMS
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT user_id,team_name FROM profiles WHERE guild_id=?", (guild_id,))
        current = list(await cursor.fetchall())
        if mode == "replace":
            for old in current:
                if old["user_id"] not in incoming_ids:
                    released.append((old["user_id"], old["team_name"]))
                    await conn.execute(
                        "DELETE FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, old["user_id"])
                    )
        for row in rows:
            external_team_id = franchise_by_name.get(row.team_name.casefold())
            await conn.execute(
                """DELETE FROM profiles WHERE guild_id=? AND lower(team_name)=lower(?) AND user_id<>?""",
                (guild_id, row.team_name, row.user_id),
            )
            await conn.execute(
                """INSERT INTO profiles
                   (guild_id,user_id,team_name,external_team_id,twitch,youtube,approved,approved_by,
                    assignment_source,assigned_at,updated_at)
                   VALUES (?,?,?,?,?,?,1,NULL,'csv',?,?)
                   ON CONFLICT(guild_id,user_id) DO UPDATE SET
                   team_name=excluded.team_name,external_team_id=excluded.external_team_id,
                   twitch=excluded.twitch,youtube=excluded.youtube,
                   approved=1,approved_by=NULL,assignment_source='csv',assigned_at=excluded.assigned_at,
                   updated_at=excluded.updated_at""",
                (
                    guild_id, row.user_id, row.team_name, external_team_id,
                    row.twitch, row.youtube, now, now,
                ),
            )
            await conn.execute(
                "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
                (guild_id, season, row.team_name),
            )
            await ensure_participant(
                conn, guild_id=guild_id, season=season, user_id=row.user_id, team_name=row.team_name
            )
        if mode == "replace":
            for team in team_names:
                cursor = await conn.execute(
                    "SELECT 1 FROM profiles WHERE guild_id=? AND lower(team_name)=lower(?)", (guild_id, team)
                )
                if not await cursor.fetchone():
                    await conn.execute(
                        """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                           VALUES (?,?,?,'Available after member import',?)
                           ON CONFLICT(guild_id,season,team_name) DO UPDATE SET updated_at=excluded.updated_at""",
                        (guild_id, season, team, now),
                    )
        if mode == "replace":
            await conn.execute(
                """UPDATE matchups SET away_user_id=NULL,home_user_id=NULL,updated_at=?
                   WHERE guild_id=? AND season=? AND status NOT IN
                   ('complete','force_home','force_away','fair_sim')""",
                (now, guild_id, season),
            )
        else:
            for row in rows:
                await conn.execute(
                    """UPDATE matchups SET
                       away_user_id=CASE WHEN away_user_id=? THEN NULL ELSE away_user_id END,
                       home_user_id=CASE WHEN home_user_id=? THEN NULL ELSE home_user_id END,
                       updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
                       ('complete','force_home','force_away','fair_sim')""",
                    (row.user_id, row.user_id, now, guild_id, season),
                )
        for row in rows:
            await conn.execute(
                """UPDATE matchups SET
                   away_user_id=CASE WHEN lower(away_team)=lower(?) THEN ? ELSE away_user_id END,
                   home_user_id=CASE WHEN lower(home_team)=lower(?) THEN ? ELSE home_user_id END,
                   updated_at=? WHERE guild_id=? AND season=? AND status NOT IN
                   ('complete','force_home','force_away','fair_sim')""",
                (row.team_name, row.user_id, row.team_name, row.user_id, now, guild_id, season),
            )
        await conn.commit()
    return released


class ConfirmMemberImportView(discord.ui.View):
    def __init__(self, db: Database, rows: tuple[MemberImportRow, ...], mode: str, author_id: int):
        super().__init__(timeout=600)
        self.db, self.rows, self.mode, self.author_id = db, rows, mode, author_id

    @discord.ui.button(label="Confirm Member Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This preview belongs to another Commissioner.", ephemeral=True)
            return
        settings = await self.db.settings(interaction.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message("Only a Commissioner can confirm this import.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            fresh = await validate_import_conflicts(
                self.db, interaction.guild_id, settings["season"],
                MemberImportPreview(self.rows, (), ()), self.mode,
            )
            if fresh.errors:
                await interaction.edit_original_response(
                    content="The import changed and is no longer safe:\n" + "\n".join(fresh.errors[:10]), view=None
                )
                return
            released = await apply_member_import(
                self.db, interaction.guild_id, settings["season"], self.rows, self.mode
            )
        except Exception:
            log.exception("Member import failed for guild %s", interaction.guild_id)
            await interaction.edit_original_response(
                content=(
                    "The member import failed before completion. No partial CSV assignment "
                    "changes were saved. Check the file format and try again.\n\n"
                    + MEMBER_CSV_FORMAT
                ),
                view=None,
            )
            return
        await self.db.audit(
            interaction.guild_id, interaction.user.id, "members_imported",
            details={"mode": self.mode, "assignments": len(self.rows), "released": len(released)},
        )
        errors: list[str] = []
        for user_id, team in released:
            member = interaction.guild.get_member(user_id)
            if member:
                try:
                    await remove_team_role(self.db, member, team)
                except Exception as exc:
                    log.exception(
                        "Could not remove imported team role for guild %s user %s",
                        interaction.guild_id,
                        user_id,
                    )
                    errors.append(f"release {user_id}/{team}: {exc}")
        for row in self.rows:
            try:
                errors.extend(await sync_assignment_discord(
                    interaction.client, self.db, interaction.guild,
                    Assignment(row.user_id, row.team_name, row.twitch, row.youtube, "csv"),
                ))
            except Exception as exc:
                log.exception(
                    "Could not sync imported assignment for guild %s user %s",
                    interaction.guild_id,
                    row.user_id,
                )
                errors.append(f"assignment {row.user_id}/{row.team_name}: {exc}")
        from .open_teams_ui import refresh_open_teams_panel
        try:
            await refresh_open_teams_panel(interaction.client, self.db, interaction.guild_id)
        except Exception as exc:
            log.exception(
                "Could not refresh Open Teams after member import for guild %s",
                interaction.guild_id,
            )
            errors.append(f"Open Teams refresh: {exc}")
        button.disabled = True
        await interaction.edit_original_response(view=self)
        message = f"Imported **{len(self.rows)}** assignments in **{self.mode}** mode; released **{len(released)}** owner(s)."
        if errors:
            message += "\n\nDiscord sync warnings (repair with `/syncmemberroles`):\n" + "\n".join(errors[:8])
        await interaction.followup.send(message[:1900], ephemeral=True)

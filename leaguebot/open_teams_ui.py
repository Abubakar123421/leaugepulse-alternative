from __future__ import annotations

import math
import re

import discord

from .checks import is_commissioner
from .db import Database
from .helpers import iso_now
from .ownership import (
    OwnershipError,
    decide_team_claim,
    initialize_open_teams,
    request_team_claim,
    sync_assignment_discord,
)
from .registration import normalize_team_name
from .team_roles import active_franchises, assign_team_role
from .team_emojis import team_emoji


async def open_team_statuses(
    db: Database, guild_id: int, season: str
) -> list[tuple[str, str, bool]]:
    cards = await _team_card_data(db, guild_id, season)
    return [(card["team_name"], card["status_text"], card["is_open"]) for card in cards]


async def open_teams_embed(db: Database, guild_id: int, season: str) -> discord.Embed:
    statuses = await open_team_statuses(db, guild_id, season)
    open_count = sum(is_open for _, _, is_open in statuses)
    pending_count = sum(status.startswith("Pending") for _, status, _ in statuses)
    owned_count = sum(status.startswith("Owner") for _, status, _ in statuses)
    embed = discord.Embed(
        title="\N{AMERICAN FOOTBALL} Open Teams",
        description=(
            "Every franchise has an individual roster card below. Use **View Team** "
            "to inspect every imported player privately, or **Claim Team** to request "
            "an available franchise. A commissioner must approve each request."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="Open", value=str(open_count))
    embed.add_field(name="Owned", value=str(owned_count))
    embed.add_field(name="Pending", value=str(pending_count))
    embed.set_footer(text=f"{len(statuses)} franchises · Cards update automatically after ownership changes")
    return embed


async def _team_card_data(db: Database, guild_id: int, season: str) -> list[dict]:
    franchises = await active_franchises(db, guild_id, season)
    profiles = await db.fetchall(
        "SELECT user_id,team_name,external_team_id,approved FROM profiles WHERE guild_id=?",
        (guild_id,),
    )
    owners_by_id = {
        str(row["external_team_id"]): row for row in profiles if row["external_team_id"]
    }
    owners_by_name = {normalize_team_name(row["team_name"]): row for row in profiles}
    open_rows = await db.fetchall(
        "SELECT team_name FROM open_rosters WHERE guild_id=? AND season=?",
        (guild_id, season),
    )
    opened = {normalize_team_name(row["team_name"]) for row in open_rows}
    players = await db.fetchall(
        """SELECT external_team_id,full_name,position,overall FROM roster_players
           WHERE guild_id=? AND season=?
           ORDER BY external_team_id,overall DESC,full_name""",
        (guild_id, season),
    )
    grouped: dict[str, list] = {}
    for player in players:
        grouped.setdefault(str(player["external_team_id"]), []).append(player)

    result: list[dict] = []
    for franchise in franchises:
        external_id = str(franchise["external_team_id"])
        team_name = franchise["team_name"]
        owner = owners_by_id.get(external_id) or owners_by_name.get(normalize_team_name(team_name))
        if owner:
            if owner["approved"]:
                status_text = f"Owner: <@{owner['user_id']}>"
                status_label = "Claimed"
            else:
                status_text = f"Pending: <@{owner['user_id']}>"
                status_label = "Pending approval"
            is_open = False
        elif normalize_team_name(team_name) in opened:
            status_text = "Open — claim now"
            status_label = "Available"
            is_open = True
        else:
            status_text = "Closed by Commissioner"
            status_label = "Closed"
            is_open = False
        roster = grouped.get(external_id, [])
        result.append({
            "external_team_id": external_id,
            "team_name": team_name,
            "abbreviation": franchise.get("abbreviation") if isinstance(franchise, dict) else franchise["abbreviation"],
            "owner": owner,
            "status_text": status_text,
            "status_label": status_label,
            "is_open": is_open,
            "roster": roster,
        })
    return result


def _team_card_embed(card: dict, season: str, emoji: discord.Emoji | None = None) -> discord.Embed:
    color = discord.Color.green() if card["is_open"] else (
        discord.Color.gold() if card["status_label"].startswith("Pending") else discord.Color.blurple()
    )
    abbreviation = card.get("abbreviation") or "TEAM"
    emoji_prefix = f"{emoji} " if emoji else ""
    embed = discord.Embed(
        title=f"{emoji_prefix}{card['team_name']} ({abbreviation})",
        color=color,
    )
    embed.add_field(name="Owner", value=card["status_text"], inline=False)
    roster = card["roster"]
    if roster:
        preview = "\n".join(
            f"`{row['position']:<3}` **{row['full_name']}** · OVR {row['overall'] if row['overall'] is not None else '—'}"
            for row in roster[:5]
        )
        if len(roster) > 5:
            preview += f"\n…and {len(roster) - 5} more players"
    else:
        preview = "No roster snapshot has been imported for this franchise."
    embed.add_field(name=f"Roster · {len(roster)} players", value=preview[:1024], inline=False)
    embed.add_field(name="Claim Status", value=card["status_label"], inline=False)
    embed.set_footer(text=f"Season {season} · Ownership lasts for the full season")
    return embed


def _roster_page_embed(team_name: str, season: str, rows: list[dict], page: int) -> discord.Embed:
    page_size = 15
    pages = max(1, math.ceil(len(rows) / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    selected = rows[start:start + page_size]
    lines = []
    for row in selected:
        overall = row["overall"] if row["overall"] is not None else "—"
        jersey = f"#{row['jersey_number']} " if row.get("jersey_number") else ""
        dev = f" · Dev {row['dev_trait']}" if row.get("dev_trait") not in (None, "") else ""
        if row.get("is_on_ir"):
            health = f" · IR{f' ({row['injury_type']})' if row.get('injury_type') else ''}"
        elif row.get("is_practice_squad"):
            health = " · Practice Squad"
        else:
            health = " · Active" if row.get("is_active", 1) else " · Inactive"
        contract = ""
        if row.get("contract_years_left") is not None:
            salary = row.get("contract_salary")
            money = f" · ${salary / 1_000_000:.1f}M" if salary is not None else ""
            contract = f" · {row['contract_years_left']}yr{money}"
        lines.append(
            f"`{row['position']:<3}` **{jersey}{row['full_name']}** · OVR {overall}"
            f"{dev}{health}{contract}"
        )
    embed = discord.Embed(
        title=f"{team_name} Full Roster",
        description="\n".join(lines) or "No roster players were imported.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Season {season} · {len(rows)} players · Page {page + 1}/{pages}")
    return embed

class RosterPagerView(discord.ui.View):
    def __init__(self, requester_id: int, team_name: str, season: str, rows: list[dict]):
        super().__init__(timeout=600)
        self.requester_id = requester_id
        self.team_name = team_name
        self.season = season
        self.rows = rows
        self.page = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        pages = max(1, math.ceil(len(self.rows) / 15))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Open your own roster viewer from the team card.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=_roster_page_embed(self.team_name, self.season, self.rows, self.page),
            view=self,
        )

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=_roster_page_embed(self.team_name, self.season, self.rows, self.page),
            view=self,
        )


class ViewRosterCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaguebot:team-card:roster:(?P<guild_id>\d+):(?P<team_id>[^:]+)",
):
    def __init__(self, guild_id: int, team_id: str):
        self.guild_id = guild_id
        self.team_id = team_id
        super().__init__(discord.ui.Button(
            label="View Team",
            emoji="\N{CLIPBOARD}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"leaguebot:team-card:roster:{guild_id}:{team_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["guild_id"]), match["team_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This team card belongs to another server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db: Database = interaction.client.db
        settings = await db.settings(self.guild_id)
        franchise = await db.fetchone(
            """SELECT team_name FROM franchises WHERE guild_id=? AND season=?
               AND external_team_id=?""",
            (self.guild_id, settings["season"], self.team_id),
        )
        team_name = franchise["team_name"] if franchise else self.team_id
        rows = [dict(row) for row in await db.fetchall(
            """SELECT full_name,position,overall,jersey_number,dev_trait,
                      is_active,is_on_ir,is_practice_squad,injury_type,
                      contract_years_left,contract_salary
               FROM roster_players
               WHERE guild_id=? AND season=? AND external_team_id=?
               ORDER BY position,overall DESC,full_name""",
            (self.guild_id, settings["season"], self.team_id),
        )]
        view = RosterPagerView(
            interaction.user.id, team_name, settings["season"], rows
        )
        await interaction.edit_original_response(
            embed=_roster_page_embed(team_name, settings["season"], rows, 0),
            view=view,
        )


class ClaimTeamCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaguebot:team-card:claim:(?P<guild_id>\d+):(?P<team_id>[^:]+)",
):
    def __init__(self, guild_id: int, team_id: str):
        self.guild_id = guild_id
        self.team_id = team_id
        super().__init__(discord.ui.Button(
            label="Claim Team",
            emoji="\N{RAISED HAND}",
            style=discord.ButtonStyle.success,
            custom_id=f"leaguebot:team-card:claim:{guild_id}:{team_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["guild_id"]), match["team_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or not interaction.guild:
            await interaction.response.send_message(
                "This team card belongs to another server.", ephemeral=True
            )
            return
        db: Database = interaction.client.db
        settings = await db.settings(self.guild_id)
        audit_channel = interaction.guild.get_channel(settings.get("audit_channel_id") or 0)
        if not isinstance(audit_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Team claims are paused until a commissioner configures the audit channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        franchise = await db.fetchone(
            """SELECT team_name FROM franchises WHERE guild_id=? AND season=?
               AND external_team_id=?""",
            (self.guild_id, settings["season"], self.team_id),
        )
        if not franchise:
            await interaction.followup.send("That team is no longer available.", ephemeral=True)
            return
        try:
            assignment = await request_team_claim(
                db, self.guild_id, settings["season"], interaction.user.id,
                franchise["team_name"],
            )
        except OwnershipError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await refresh_open_team_card(
            interaction.client, db, self.guild_id,
            assignment.external_team_id or assignment.team_name,
        )
        commissioner_mention = (
            f"<@&{settings['commissioner_role_id']}>"
            if settings.get("commissioner_role_id") else "Commissioners"
        )
        embed = discord.Embed(
            title="Pending Team Claim",
            description=(
                f"**Member:** {interaction.user.mention}\n"
                f"**Team:** {assignment.team_name}\n\n"
                "Approve or deny this request below."
            ),
            color=discord.Color.gold(),
        )
        try:
            await audit_channel.send(
                commissioner_mention,
                embed=embed,
                view=ClaimReviewView(self.guild_id, interaction.user.id),
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            await decide_team_claim(
                db, self.guild_id, settings["season"], interaction.user.id,
                approved=False, decided_by=interaction.client.user.id,
            )
            await refresh_open_team_card(
                interaction.client, db, self.guild_id,
                assignment.external_team_id or assignment.team_name,
            )
            await interaction.followup.send(
                "I could not post the commissioner review card, so the claim was cancelled.",
                ephemeral=True,
            )
            return
        await db.audit(
            self.guild_id, interaction.user.id, "team_claim_requested",
            target_type="team", target_id=assignment.team_name,
        )
        await interaction.followup.send(
            f"Your request for **{assignment.team_name}** is pending commissioner approval.",
            ephemeral=True,
        )


class ClaimReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaguebot:claim-review:(?P<guild_id>\d+):(?P<user_id>\d+):(?P<action>approve|deny)",
):
    def __init__(self, guild_id: int, user_id: int, action: str):
        self.guild_id = guild_id
        self.user_id = user_id
        self.action = action
        approved = action == "approve"
        super().__init__(discord.ui.Button(
            label="Approve" if approved else "Deny",
            style=discord.ButtonStyle.success if approved else discord.ButtonStyle.danger,
            custom_id=f"leaguebot:claim-review:{guild_id}:{user_id}:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["guild_id"]), int(match["user_id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or not interaction.guild:
            await interaction.response.send_message(
                "This review belongs to another server.", ephemeral=True
            )
            return
        db: Database = interaction.client.db
        settings = await db.settings(self.guild_id)
        if not await is_commissioner(interaction, settings):
            await interaction.response.send_message(
                "Only commissioners can decide team claims.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        approved = self.action == "approve"
        member = interaction.guild.get_member(self.user_id)
        pending = await db.fetchone(
            "SELECT * FROM profiles WHERE guild_id=? AND user_id=? AND approved=0",
            (self.guild_id, self.user_id),
        )
        if not pending:
            await interaction.followup.send("This claim is no longer pending.", ephemeral=True)
            return
        if approved:
            if not member:
                await interaction.followup.send(
                    "That member is no longer in this server. Deny the claim to reopen the team.",
                    ephemeral=True,
                )
                return
            try:
                await assign_team_role(db, member, pending["team_name"])
            except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(
                    f"The claim was not approved because the team role could not be assigned: {exc}",
                    ephemeral=True,
                )
                return
        try:
            assignment = await decide_team_claim(
                db, self.guild_id, settings["season"], self.user_id,
                approved=approved, decided_by=interaction.user.id,
            )
        except OwnershipError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        errors: list[str] = []
        if approved:
            errors = await sync_assignment_discord(
                interaction.client, db, interaction.guild, assignment
            )
        await refresh_open_team_card(
            interaction.client, db, self.guild_id,
            assignment.external_team_id or assignment.team_name,
        )
        action_word = "approved" if approved else "denied"
        await db.audit(
            self.guild_id, interaction.user.id, f"team_claim_{action_word}",
            target_type="user", target_id=str(self.user_id),
            details={"team": assignment.team_name, "role_sync_errors": errors},
        )
        resolved_embed = discord.Embed(
            title=f"Team Claim {action_word.title()}",
            description=(
                f"**Member:** <@{self.user_id}>\n**Team:** {assignment.team_name}\n"
                f"**Decided by:** {interaction.user.mention}"
            ),
            color=discord.Color.green() if approved else discord.Color.red(),
        )
        try:
            await interaction.message.edit(embed=resolved_embed, view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        if member:
            try:
                await member.send(
                    f"Your request for **{assignment.team_name}** was {action_word}."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        suffix = " Some Discord updates need `/syncmemberroles`." if errors else ""
        await interaction.followup.send(
            f"The claim was {action_word}.{suffix}", ephemeral=True
        )


class ClaimReviewView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=None)
        self.add_item(ClaimReviewButton(guild_id, user_id, "approve"))
        self.add_item(ClaimReviewButton(guild_id, user_id, "deny"))

class TeamCardView(discord.ui.View):
    def __init__(self, guild_id: int, team_id: str, *, is_open: bool):
        super().__init__(timeout=None)
        self.add_item(ViewRosterCardButton(guild_id, team_id))
        if is_open:
            self.add_item(ClaimTeamCardButton(guild_id, team_id))


async def post_open_teams_panel(
    client: discord.Client, db: Database, guild: discord.Guild, channel: discord.TextChannel
) -> discord.Message:
    settings = await db.settings(guild.id)
    season = settings["season"]
    await initialize_open_teams(db, guild.id, season)
    await _retire_old_panel(db, guild, settings)
    header = await channel.send(embed=await open_teams_embed(db, guild.id, season))
    try:
        await header.pin(reason="Persistent league roster directory")
    except (discord.Forbidden, discord.HTTPException):
        pass
    await db.update_settings(
        guild.id, open_teams_channel_id=channel.id, open_teams_message_id=header.id
    )
    cards = await _team_card_data(db, guild.id, season)
    for card in cards:
        message = await channel.send(
            embed=_team_card_embed(
                card, season, await team_emoji(db, guild, season, card["team_name"])
            ),
            view=TeamCardView(guild.id, card["external_team_id"], is_open=card["is_open"]),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.execute(
            """INSERT INTO open_team_cards
               (guild_id,season,external_team_id,team_name,channel_id,message_id,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(guild_id,season,external_team_id)
               DO UPDATE SET team_name=excluded.team_name,channel_id=excluded.channel_id,
               message_id=excluded.message_id,updated_at=excluded.updated_at""",
            (
                guild.id, season, card["external_team_id"], card["team_name"],
                channel.id, message.id, iso_now(),
            ),
        )
    return header


async def _retire_old_panel(db: Database, guild: discord.Guild, settings: dict) -> None:
    old_messages: set[tuple[int, int]] = set()
    if settings.get("open_teams_channel_id") and settings.get("open_teams_message_id"):
        old_messages.add((settings["open_teams_channel_id"], settings["open_teams_message_id"]))
    for row in await db.fetchall(
        "SELECT channel_id,message_id FROM open_team_cards WHERE guild_id=?", (guild.id,)
    ):
        old_messages.add((row["channel_id"], row["message_id"]))
    for channel_id, message_id in old_messages:
        old_channel = guild.get_channel(channel_id)
        if not isinstance(old_channel, discord.TextChannel):
            continue
        try:
            message = await old_channel.fetch_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await db.execute("DELETE FROM open_team_cards WHERE guild_id=?", (guild.id,))


async def refresh_open_team_card(
    client: discord.Client,
    db: Database,
    guild_id: int,
    team_reference: str,
) -> None:
    """Refresh one franchise card and the summary without touching all 32 cards."""
    guild = client.get_guild(guild_id)
    if not guild:
        return
    settings = await db.settings(guild_id)
    channel = guild.get_channel(settings.get("open_teams_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel):
        return
    season = settings["season"]
    cards = await _team_card_data(db, guild_id, season)
    normalized = normalize_team_name(str(team_reference))
    card = next(
        (
            item for item in cards
            if str(item["external_team_id"]) == str(team_reference)
            or normalize_team_name(item["team_name"]) == normalized
        ),
        None,
    )
    if card is None:
        return
    mapping = await db.fetchone(
        """SELECT * FROM open_team_cards WHERE guild_id=? AND season=?
           AND external_team_id=?""",
        (guild_id, season, card["external_team_id"]),
    )
    if mapping:
        card_channel = guild.get_channel(mapping["channel_id"])
        if isinstance(card_channel, discord.TextChannel):
            try:
                message = card_channel.get_partial_message(mapping["message_id"])
                await message.edit(
                    embed=_team_card_embed(
                card, season, await team_emoji(db, guild, season, card["team_name"])
            ),
                    view=TeamCardView(
                        guild_id,
                        card["external_team_id"],
                        is_open=card["is_open"],
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                mapping = None
            except (discord.Forbidden, discord.HTTPException):
                pass
    if not mapping:
        message = await channel.send(
            embed=_team_card_embed(
                card, season, await team_emoji(db, guild, season, card["team_name"])
            ),
            view=TeamCardView(
                guild_id, card["external_team_id"], is_open=card["is_open"]
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.execute(
            """INSERT INTO open_team_cards
               (guild_id,season,external_team_id,team_name,channel_id,message_id,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(guild_id,season,external_team_id)
               DO UPDATE SET team_name=excluded.team_name,channel_id=excluded.channel_id,
               message_id=excluded.message_id,updated_at=excluded.updated_at""",
            (
                guild_id,
                season,
                card["external_team_id"],
                card["team_name"],
                channel.id,
                message.id,
                iso_now(),
            ),
        )
    if settings.get("open_teams_message_id"):
        try:
            header = channel.get_partial_message(settings["open_teams_message_id"])
            await header.edit(embed=await open_teams_embed(db, guild_id, season), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

async def refresh_open_teams_panel(client: discord.Client, db: Database, guild_id: int) -> None:
    guild = client.get_guild(guild_id)
    if not guild:
        return
    settings = await db.settings(guild_id)
    channel = guild.get_channel(settings.get("open_teams_channel_id") or 0)
    if not isinstance(channel, discord.TextChannel) or not settings.get("open_teams_message_id"):
        return
    season = settings["season"]
    cards = await _team_card_data(db, guild_id, season)
    mappings = await db.fetchall(
        """SELECT * FROM open_team_cards WHERE guild_id=? AND season=?
           ORDER BY team_name""",
        (guild_id, season),
    )
    desired_ids = {card["external_team_id"] for card in cards}
    mapped_ids = {str(row["external_team_id"]) for row in mappings}
    if desired_ids != mapped_ids:
        await post_open_teams_panel(client, db, guild, channel)
        return
    try:
        header = channel.get_partial_message(settings["open_teams_message_id"])
        await header.edit(embed=await open_teams_embed(db, guild_id, season), view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        await post_open_teams_panel(client, db, guild, channel)
        return
    by_id = {str(row["external_team_id"]): row for row in mappings}
    for card in cards:
        mapping = by_id[card["external_team_id"]]
        card_channel = guild.get_channel(mapping["channel_id"])
        if not isinstance(card_channel, discord.TextChannel):
            continue
        try:
            message = card_channel.get_partial_message(mapping["message_id"])
            await message.edit(
                embed=_team_card_embed(
                card, season, await team_emoji(db, guild, season, card["team_name"])
            ),
                view=TeamCardView(guild_id, card["external_team_id"], is_open=card["is_open"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            message = await channel.send(
                embed=_team_card_embed(
                card, season, await team_emoji(db, guild, season, card["team_name"])
            ),
                view=TeamCardView(guild_id, card["external_team_id"], is_open=card["is_open"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await db.execute(
                """UPDATE open_team_cards SET channel_id=?,message_id=?,updated_at=?
                   WHERE guild_id=? AND season=? AND external_team_id=?""",
                (channel.id, message.id, iso_now(), guild_id, season, card["external_team_id"]),
            )
        except (discord.Forbidden, discord.HTTPException):
            continue

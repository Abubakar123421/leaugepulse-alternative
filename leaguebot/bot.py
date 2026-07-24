from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

from .checks import is_commissioner, require_commissioner
from .awards import (
    AWARD_CATEGORIES, SeasonAwardsApprovalView, awards_ready, ensure_award_suggestions,
    season_awards_embed, set_season_award,
)
from .game_of_week import post_game_of_week
from .team_emojis import emoji_named, sync_team_emojis
from .weekly_content import upsert_week_mvp
from .channel_workflow import (
    create_week_matchup_channels, handle_matchup_message, handle_raw_reaction,
    refresh_matchup_message,
)
from .commissioner_ui import (
    AdvanceWeekConfirmationView,
    CommissionerWeekDashboardView,
    OutcomeConfirmationView,
)
from .ai import AIService, deterministic_rankings
from .config import Config
from .db import Database
from .help_ui import HelpView
from .helpers import FINAL_STATUSES, iso_now, next_deadline, status_label, valid_timezone
from .imports import college_template, parse_schedule_csv
from .progression import ensure_participant, level_for_xp, next_level_xp, win_ratio
from .registration import normalize_team_name, registration_rejection_embed, registration_state
from .roster_ui import TeamReleaseConfirmationView
from .result_ui import (
    CommissionerResultActionButton,
    CommissionerResultConfirmationView,
    OpponentResultDecisionButton,
    restore_pending_result_reviews,
)
from .season_lifecycle import season_close_preview
from .season_ui import (
    SeasonCleanupRetryView,
    SeasonCloseConfirmationView,
    season_close_embed,
)
from .schedule_ui import ScheduleDecisionButton
from .services import ReminderService, StreamService, WeekRolloverService, make_backup
from .startup_migrations import backfill_active_leagues
from .team_roles import (
    active_team_names, assign_team_role, ensure_madden_team_roles, remove_team_role,
)
from .views import ConfirmImportView
from .league_import import (
    ConfirmFixtureImportView, ConfirmRosterImportView,
    parse_fixture_import, parse_roster_import, roster_team_summary,
)
from .member_import import (
    ConfirmMemberImportView, parse_member_csv, validate_import_conflicts,
)
from .open_teams_ui import (
    ClaimTeamCardButton, ViewRosterCardButton, post_open_teams_panel, refresh_open_team_card, refresh_open_teams_panel,
)
from .ownership import (
    OwnershipError, claim_team, initialize_open_teams, sync_all_member_roles,
    sync_assignment_discord,
)

log = logging.getLogger(__name__)

WEEK_ACTION_CHOICES = (
    ("Approve submitted score — requires screenshot evidence", "complete"),
    ("Force win: home team — award the home team", "force_home"),
    ("Force win: away team — award the away team", "force_away"),
    ("Fair sim — use a neutral simulation", "fair_sim"),
    ("Advance — close this week and move to the next", "advance"),
)


async def week_action_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    query = current.casefold().strip()
    return [
        app_commands.Choice(name=label, value=value)
        for label, value in WEEK_ACTION_CHOICES
        if not query or query in label.casefold() or query in value
    ][:25]


async def send_week_dashboard(followup, embed: discord.Embed, view) -> None:
    if view is None:
        await followup.send(embed=embed)
    else:
        await followup.send(embed=embed, view=view, ephemeral=True)


async def refresh_team_emoji_posts(
    bot: discord.Client, db: Database, guild: discord.Guild, season: str
) -> None:
    await refresh_open_teams_panel(bot, db, guild.id)
    rows = await db.fetchall(
        """SELECT id FROM matchups
           WHERE guild_id=? AND season=? AND channel_id IS NOT NULL""",
        (guild.id, season),
    )
    for row in rows:
        await refresh_matchup_message(bot, db, row["id"])

class LeagueBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.db = Database(config.database_path)
        self.tree = app_commands.CommandTree(self)
        self._week_locks: dict[tuple[int, str, int], asyncio.Lock] = {}
        self._roles_repaired = False
        self.reminders = ReminderService(self, self.db, config.reminder_poll_seconds)
        self.week_rollovers = WeekRolloverService(self, self.db)
        self.ai = AIService(self, self.db, config)
        self.streams = StreamService(
            self, self.db, config.stream_poll_seconds,
            config.twitch_client_id, config.twitch_client_secret, config.youtube_api_key,
        )

    def week_lock(self, guild_id: int, season: str, week: int) -> asyncio.Lock:
        key = (guild_id, season, week)
        return self._week_locks.setdefault(key, asyncio.Lock())
    async def setup_hook(self) -> None:
        await self.db.initialize()
        await backfill_active_leagues(self.db)
        await self.ai.start()
        register_commands(self)
        self.add_dynamic_items(ClaimTeamCardButton)
        self.add_dynamic_items(ViewRosterCardButton)
        self.add_dynamic_items(ScheduleDecisionButton)
        self.add_dynamic_items(OpponentResultDecisionButton)
        self.add_dynamic_items(CommissionerResultActionButton)
        await self.tree.sync()
        self.reminders.start()
        await self.week_rollovers.start()
        self.streams.start()

    async def close(self) -> None:
        await self.reminders.close()
        await self.week_rollovers.close()
        await self.streams.close()
        await self.ai.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s), serving %d guild(s)", self.user, self.user.id, len(self.guilds))
        if self._roles_repaired:
            return
        self._roles_repaired = True
        for guild in self.guilds:
            settings = await self.db.settings(guild.id)
            if not settings.get("commissioner_role_id"):
                continue
            created, errors = await ensure_madden_team_roles(guild, self.db)
            log.info(
                "Madden team roles checked for guild %s: %d created, %d error(s)",
                guild.id, created, len(errors),
            )
            repaired = await restore_pending_result_reviews(self, self.db, guild.id)
            emoji_count, emoji_missing = await sync_team_emojis(
                self.db, guild, settings["season"]
            )
            log.info(
                "Team emojis checked for guild %s: %d matched, %d missing",
                guild.id, emoji_count, len(emoji_missing),
            )
            await refresh_team_emoji_posts(
                self, self.db, guild, settings["season"]
            )
            if repaired:
                log.info(
                    "Restored %d pending result review card(s) for guild %s",
                    repaired,
                    guild.id,
                )


    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await handle_raw_reaction(self, self.db, payload)

    async def on_message(self, message: discord.Message) -> None:
        await handle_matchup_message(self, self.db, message)
    async def on_member_remove(self, member: discord.Member) -> None:
        profile = await self.db.fetchone(
            """SELECT * FROM profiles
               WHERE guild_id=? AND user_id=? AND approved=1""",
            (member.guild.id, member.id),
        )
        if not profile:
            return
        settings = await self.db.settings(member.guild.id)
        audit = member.guild.get_channel(settings.get("audit_channel_id") or 0)
        if isinstance(audit, discord.TextChannel):
            try:
                await audit.send(
                    embed=discord.Embed(
                        title="Approved Team Owner Left the Server",
                        description=(
                            f"**Member:** {member} (`{member.id}`)\n"
                            f"**Team:** {profile['team_name']}\n\n"
                            "The assignment was preserved to prevent accidental data loss. "
                            "A Commissioner can use `/team-release` after confirming the departure."
                        ),
                        color=discord.Color.red(),
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

def guild_only(interaction: discord.Interaction) -> bool:
    return interaction.guild_id is not None and interaction.guild is not None


async def deny_dm(interaction: discord.Interaction) -> bool:
    if guild_only(interaction):
        return False
    await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    return True


def register_commands(bot: LeagueBot) -> None:
    tree, db = bot.tree, bot.db

    @tree.command(name="setup", description="Create or connect this server's league channels.")
    @app_commands.describe(
        league_name="Name displayed by the bot",
        game="Madden 26, College Football 27, or a future season",
        season="Season identifier",
        timezone="IANA timezone such as America/New_York",
        commissioner_role="Role allowed to manage the league",
    )
    async def setup(
        interaction: discord.Interaction, league_name: str, game: str, season: str,
        timezone: str, commissioner_role: discord.Role,
    ) -> None:
        if await deny_dm(interaction):
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Manage Server permission is required.", ephemeral=True)
            return
        if commissioner_role.is_default():
            await interaction.response.send_message(
                "Choose a dedicated Commissioner role instead of @everyone.", ephemeral=True
            )
            return
        if not valid_timezone(timezone):
            await interaction.response.send_message(
                "That timezone is invalid. Example: `America/New_York`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        roles_created, role_errors = await ensure_madden_team_roles(guild, db)
        current = await db.settings(guild.id)
        category = guild.get_channel(current.get("category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(f"{league_name} League")
        matchup_category = guild.get_channel(current.get("matchup_category_id") or 0)
        if not isinstance(matchup_category, discord.CategoryChannel):
            matchup_category = await guild.create_category("Weekly Matchups")

        async def channel(current_id: int | None, name: str) -> discord.TextChannel:
            existing = guild.get_channel(current_id or 0)
            if isinstance(existing, discord.TextChannel):
                return existing
            return await guild.create_text_channel(name, category=category)

        announcements = await channel(current.get("announcements_channel_id"), "announcements")
        final_scores = await channel(current.get("final_scores_channel_id"), "final-scores")
        storylines = await channel(current.get("storyline_channel_id"), "weekly-spotlight")
        trades = await channel(current.get("trade_channel_id"), "trade-block")
        open_teams = await channel(current.get("open_teams_channel_id"), "open-teams")
        polls = await channel(current.get("polls_channel_id"), "league-polls")
        recruiting = await channel(current.get("recruiting_channel_id"), "recruiting")
        transactions = await channel(current.get("transactions_channel_id"), "transactions")
        streams = await channel(current.get("streams_channel_id"), "live-streams")
        audit = await channel(current.get("audit_channel_id"), "commissioner-audit")
        audit_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            commissioner_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }
        if guild.me:
            audit_overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
            )
        await audit.edit(
            overwrites=audit_overwrites,
            reason=f"League audit access assigned to {commissioner_role.name}",
        )
        await db.update_settings(
            guild.id, league_name=league_name, game=game, season=season, timezone=timezone,
            commissioner_role_id=commissioner_role.id, category_id=category.id,
            matchup_category_id=matchup_category.id, matchups_channel_id=None,
            announcements_channel_id=announcements.id,
            final_scores_channel_id=final_scores.id, storyline_channel_id=storylines.id,
            trade_channel_id=trades.id, open_teams_channel_id=open_teams.id,
            polls_channel_id=polls.id, recruiting_channel_id=recruiting.id,
            transactions_channel_id=transactions.id, streams_channel_id=streams.id,
            audit_channel_id=audit.id,
        )
        await db.audit(guild.id, interaction.user.id, "setup")
        await interaction.followup.send(
            f"Setup complete for **{league_name}**. I created or connected the management channels "
            f"under {category.mention}, with weekly games under {matchup_category.mention}. Madden team roles created: **{roles_created}**. "
            + (f"Role errors: {len(role_errors)}. " if role_errors else "")
            + "Use `/settings` to review the configuration.",
            ephemeral=True,
        )

    async def set_text_destination(
        interaction: discord.Interaction,
        *,
        field: str,
        label: str,
        channel: discord.TextChannel,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await db.update_settings(interaction.guild_id, **{field: channel.id})
        await db.audit(
            interaction.guild_id,
            interaction.user.id,
            "destination_updated",
            target_type="channel",
            target_id=str(channel.id),
            details={"destination": field},
        )
        permissions = channel.permissions_for(interaction.guild.me)
        warning = ""
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            warning = (
                "\n⚠️ I saved it, but my role still needs View Channel, "
                "Send Messages, and Embed Links there."
            )
        await interaction.response.send_message(
            f"**{label}** will now post in {channel.mention}.{warning}",
            ephemeral=True,
        )

    @tree.command(name="setmatchcategory", description="Choose the category for weekly matchup channels.")
    async def set_match_category(
        interaction: discord.Interaction, category: discord.CategoryChannel
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await db.update_settings(interaction.guild_id, matchup_category_id=category.id)
        active = await db.fetchall(
            """SELECT channel_id FROM matchups WHERE guild_id=? AND season=?
               AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (interaction.guild_id, settings["season"]),
        )
        moved = 0
        move_errors = 0
        for row in active:
            game_channel = interaction.guild.get_channel(row["channel_id"] or 0)
            if isinstance(game_channel, discord.TextChannel) and game_channel.category_id != category.id:
                try:
                    await game_channel.edit(category=category, reason="Matchup destination updated")
                    moved += 1
                except (discord.Forbidden, discord.HTTPException):
                    move_errors += 1
        await db.audit(
            interaction.guild_id, interaction.user.id, "destination_updated",
            target_type="category", target_id=str(category.id),
            details={"destination": "matchup_category_id"},
        )
        await interaction.response.send_message(
            f"Weekly matchup channels will now be created under **{category.name}**. "
            f"Moved **{moved}** active channel(s)."
            + (f" **{move_errors}** could not be moved because of permissions." if move_errors else ""),
            ephemeral=True,
        )

    @tree.command(name="setannouncementchannel", description="Choose where league announcements are posted.")
    async def set_announcement_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="announcements_channel_id",
            label="League announcements", channel=channel,
        )

    @tree.command(name="setscorechannel", description="Choose where official final scores are posted.")
    async def set_score_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="final_scores_channel_id",
            label="Official final scores", channel=channel,
        )

    @tree.command(name="setstreamchannel", description="Choose where live stream alerts are posted.")
    async def set_stream_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="streams_channel_id",
            label="Live stream alerts", channel=channel,
        )

    @tree.command(name="setstorylinechannel", description="Choose where spotlights, rankings, and awards are posted.")
    async def set_storyline_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="storyline_channel_id",
            label="Weekly spotlight content", channel=channel,
        )

    @tree.command(name="settradechannel", description="Choose where trade-block and trade activity is posted.")
    async def set_trade_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="trade_channel_id",
            label="Trade activity", channel=channel,
        )

    @tree.command(name="settransactionchannel", description="Choose where transfers and general transactions are posted.")
    async def set_transaction_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="transactions_channel_id",
            label="Transactions and transfers", channel=channel,
        )

    @tree.command(name="setauditchannel", description="Choose the private commissioner audit channel.")
    async def set_audit_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="audit_channel_id",
            label="Commissioner audit events", channel=channel,
        )

    async def configure_open_team_panel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await post_open_teams_panel(interaction.client, db, interaction.guild, channel)
        await db.audit(
            interaction.guild_id, interaction.user.id, "open_teams_panel_configured",
            target_type="channel", target_id=str(channel.id),
            details={"message_id": message.id},
        )
        await interaction.followup.send(
            f"The persistent Open Teams dashboard is ready in {channel.mention}.", ephemeral=True
        )

    @tree.command(name="setopenteamlist", description="Configure and post the persistent Open Teams dashboard.")
    async def set_open_team_list(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await configure_open_team_panel(interaction, channel)

    @tree.command(name="setopenchannel", description="Alias for /setopenteamlist.")
    async def set_open_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await configure_open_team_panel(interaction, channel)

    @tree.command(name="setpollchannel", description="Choose where league polls will be posted.")
    async def set_poll_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="polls_channel_id",
            label="League polls", channel=channel,
        )

    @tree.command(name="setrecruitingchannel", description="Choose where recruiting updates will be posted.")
    async def set_recruiting_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await set_text_destination(
            interaction, field="recruiting_channel_id",
            label="Recruiting updates", channel=channel,
        )

    @tree.command(name="destinations", description="Show this server's configured bot destinations.")
    async def destinations(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        rows = (
            ("Weekly matchups", "matchup_category_id", True),
            ("Announcements", "announcements_channel_id", False),
            ("Final scores", "final_scores_channel_id", False),
            ("Weekly spotlight", "storyline_channel_id", False),
            ("Live streams", "streams_channel_id", False),
            ("Trade activity", "trade_channel_id", False),
            ("Transactions / transfers", "transactions_channel_id", False),
            ("Open teams", "open_teams_channel_id", False),
            ("Polls", "polls_channel_id", False),
            ("Recruiting", "recruiting_channel_id", False),
            ("Commissioner audit", "audit_channel_id", False),
        )
        lines = []
        for label, field, is_category in rows:
            target_id = settings.get(field)
            target = interaction.guild.get_channel(target_id or 0)
            if target is None:
                value = "Not configured"
            elif is_category:
                value = f"**{target.name}** (category)"
            else:
                value = target.mention
            lines.append(f"**{label}:** {value}")
        embed = discord.Embed(
            title=f"{settings['league_name']} · Automated Destinations",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Every destination is isolated to this Discord server.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @tree.command(name="help", description="Open the interactive league help center.")
    async def help_command(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        commands = list(tree.get_commands())
        view = HelpView(commands, await is_commissioner(interaction, settings))
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @tree.command(name="settings", description="View or update this server's league settings.")
    @app_commands.describe(
        current_week="Optional new current week",
        advance_weekday="Monday=0 through Sunday=6",
        advance_time="24-hour local time, for example 21:00",
        timezone="IANA timezone",
    )
    async def settings_command(
        interaction: discord.Interaction, current_week: int | None = None,
        advance_weekday: app_commands.Range[int, 0, 6] | None = None,
        advance_time: str | None = None, timezone: str | None = None,
        feature: str | None = None, feature_enabled: bool | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        changes = {
            key: value for key, value in {
                "current_week": current_week, "advance_weekday": advance_weekday,
                "advance_time": advance_time, "timezone": timezone,
            }.items() if value is not None
        }
        if feature is not None:
            normalized_feature = feature.strip().lower().replace(" ", "_")
            if normalized_feature not in settings["features"]:
                await interaction.response.send_message(
                    "Unknown module. Use trades, transfers, open_rosters, announcements, "
                    "awards, or streams.", ephemeral=True,
                )
                return
            if feature_enabled is None:
                await interaction.response.send_message(
                    "Provide feature_enabled when changing a module.", ephemeral=True
                )
                return
            updated_features = dict(settings["features"])
            updated_features[normalized_feature] = feature_enabled
            changes["features"] = updated_features
        if changes:
            if not await require_commissioner(interaction, settings):
                return
            if timezone and not valid_timezone(timezone):
                await interaction.response.send_message("Invalid IANA timezone.", ephemeral=True)
                return
            if advance_time:
                try:
                    hour, minute = map(int, advance_time.split(":"))
                    if hour not in range(24) or minute not in range(60):
                        raise ValueError
                except ValueError:
                    await interaction.response.send_message("Use HH:MM in 24-hour time.", ephemeral=True)
                    return
            await db.update_settings(interaction.guild_id, **changes)
            await db.audit(interaction.guild_id, interaction.user.id, "settings_update", details=changes)
            settings = await db.settings(interaction.guild_id)
        embed = discord.Embed(title=f"{settings['league_name']} · League Settings", color=0x5865F2)
        embed.add_field(name="Game / Season", value=f"{settings['game']} · {settings['season']}")
        embed.add_field(name="Current Week", value=str(settings["current_week"]))
        embed.add_field(
            name="Advance", value=f"weekday {settings['advance_weekday']} at "
            f"{settings['advance_time']} ({settings['timezone']})", inline=False,
        )
        enabled = [name.replace("_", " ").title() for name, value in settings["features"].items() if value]
        embed.add_field(name="Enabled Modules", value=", ".join(enabled) or "None", inline=False)
        method = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await method(embed=embed, ephemeral=True)

    async def register_team_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        settings = await db.settings(interaction.guild_id)
        state = await registration_state(db, interaction.guild_id, settings["season"])
        search = normalize_team_name(current)
        teams = [
            team for team in state.available_for(interaction.user.id)
            if not search or search in normalize_team_name(team)
        ]
        return [app_commands.Choice(name=team, value=team) for team in teams[:25]]

    @tree.command(name="register", description="Request a team and add Twitch/YouTube profile links.")
    @app_commands.autocomplete(team=register_team_autocomplete)
    async def register(
        interaction: discord.Interaction, team: str, twitch: str | None = None,
        youtube: str | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = await db.settings(interaction.guild_id)
        state = await registration_state(db, interaction.guild_id, settings["season"])
        if not state.all_teams:
            await interaction.followup.send(
                embed=registration_rejection_embed(
                    state,
                    reason=(
                        "No teams are available because a schedule has not been imported "
                        "for the current season. Ask a commissioner to run `/import-schedule` first."
                    ),
                    user_id=interaction.user.id,
                ),
                ephemeral=True,
            )
            return

        canonical_team = state.canonical(team)
        if canonical_team is None:
            await interaction.followup.send(
                embed=registration_rejection_embed(
                    state,
                    reason=(
                        f"**{team.strip()}** is not a team in the imported Season "
                        f"{settings['season']} schedule. Choose an exact team name from "
                        "Available Teams below. Letter case does not matter."
                    ),
                    user_id=interaction.user.id,
                ),
                ephemeral=True,
            )
            return

        normalized = normalize_team_name(canonical_team)
        claimed_by = state.taken.get(normalized) or state.pending.get(normalized)
        if claimed_by and claimed_by != interaction.user.id:
            claim_status = "already taken" if normalized in state.taken else "awaiting review"
            await interaction.followup.send(
                embed=registration_rejection_embed(
                    state,
                    reason=(
                        f"**{canonical_team}** is {claim_status} for another member. "
                        "Choose one of the Available Teams below."
                    ),
                    user_id=interaction.user.id,
                ),
                ephemeral=True,
            )
            return

        existing = await db.fetchone(
            "SELECT * FROM profiles WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, interaction.user.id),
        )
        if (
            existing
            and existing["approved"]
            and normalize_team_name(existing["team_name"]) == normalized
        ):
            await db.execute(
                "UPDATE profiles SET twitch=?, youtube=?, updated_at=? WHERE id=?",
                (twitch, youtube, iso_now(), existing["id"]),
            )
            await interaction.followup.send(
                f"Your **{canonical_team}** profile was updated and remains approved.",
                ephemeral=True,
            )
            return

        if (
            existing
            and existing["approved"]
            and normalize_team_name(existing["team_name"]) != normalized
        ):
            await interaction.followup.send(
                embed=registration_rejection_embed(
                    state,
                    reason=(
                        "Approved owners cannot switch teams through `/register`. "
                        "A Commissioner must use `/team-release` first so unfinished "
                        "games and ownership history are handled safely."
                    ),
                    user_id=interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if existing and normalize_team_name(existing["team_name"]) != normalized:
            await db.execute(
                """UPDATE matchups SET away_user_id=NULL, updated_at=?
                   WHERE guild_id=? AND season=? AND away_user_id=?""",
                (iso_now(), interaction.guild_id, settings["season"], interaction.user.id),
            )
            await db.execute(
                """UPDATE matchups SET home_user_id=NULL, updated_at=?
                   WHERE guild_id=? AND season=? AND home_user_id=?""",
                (iso_now(), interaction.guild_id, settings["season"], interaction.user.id),
            )

        franchise = await db.fetchone(
            """SELECT external_team_id FROM franchises WHERE guild_id=? AND season=?
               AND lower(team_name)=lower(?)""",
            (interaction.guild_id, settings["season"], canonical_team),
        )
        external_team_id = franchise["external_team_id"] if franchise else None
        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """SELECT user_id,approved FROM profiles
                   WHERE guild_id=? AND lower(team_name)=lower(?) AND user_id != ?
                   LIMIT 1""",
                (interaction.guild_id, canonical_team, interaction.user.id),
            )
            conflict = await cursor.fetchone()
            if conflict:
                await conn.rollback()
                refreshed = await registration_state(
                    db, interaction.guild_id, settings["season"]
                )
                await interaction.followup.send(
                    embed=registration_rejection_embed(
                        refreshed,
                        reason=(
                            f"**{canonical_team}** was claimed by another member while "
                            "you were submitting. Choose an available team."
                        ),
                        user_id=interaction.user.id,
                    ),
                    ephemeral=True,
                )
                return
            await conn.execute(
                """INSERT INTO profiles
                   (guild_id,user_id,team_name,external_team_id,twitch,youtube,approved,updated_at)
                   VALUES (?,?,?,?,?,?,0,?)
                   ON CONFLICT(guild_id,user_id) DO UPDATE SET
                   team_name=excluded.team_name,external_team_id=excluded.external_team_id,
                   twitch=excluded.twitch,youtube=excluded.youtube,approved=0,approved_by=NULL,
                   updated_at=excluded.updated_at""",
                (
                    interaction.guild_id,
                    interaction.user.id,
                    canonical_team,
                    external_team_id,
                    twitch,
                    youtube,
                    iso_now(),
                ),
            )
            await conn.commit()
        await db.audit(
            interaction.guild_id,
            interaction.user.id,
            "registration_requested",
            target_type="team",
            target_id=canonical_team,
        )
        audit_channel = interaction.guild.get_channel(settings.get("audit_channel_id") or 0)
        notified = False
        if isinstance(audit_channel, discord.TextChannel):
            commissioner_mention = (
                f"<@&{settings['commissioner_role_id']}>"
                if settings.get("commissioner_role_id")
                else "Commissioners"
            )
            embed = discord.Embed(
                title="New Team Registration Request",
                description=(
                    f"**Member:** {interaction.user.mention}\n"
                    f"**Team requested:** {canonical_team}\n"
                    f"**Twitch:** {twitch or 'Not provided'}\n"
                    f"**YouTube:** {youtube or 'Not provided'}"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(
                text="Use /profile-approve with this member, then choose True or False."
            )
            await audit_channel.send(
                commissioner_mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                ),
            )
            notified = True
        await interaction.followup.send(
            f"Registration saved for **{canonical_team}**. "
            + (
                "The commissioners have been notified for approval."
                if notified
                else "A commissioner must approve the team claim."
            ),
            ephemeral=True,
        )
    async def open_team_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        settings = await db.settings(interaction.guild_id)
        open_rows = await db.fetchall(
            "SELECT team_name FROM open_rosters WHERE guild_id=? AND season=?",
            (interaction.guild_id, settings["season"]),
        )
        query = normalize_team_name(current)
        teams = [row["team_name"] for row in open_rows if not query or query in normalize_team_name(row["team_name"])]
        return [app_commands.Choice(name=team, value=team) for team in sorted(teams)[:25]]

    @tree.command(name="registerteam", description="Instantly claim an available Madden team.")
    @app_commands.autocomplete(team=open_team_autocomplete)
    async def register_team(interaction: discord.Interaction, team: str) -> None:
        if await deny_dm(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await db.settings(interaction.guild_id)
        try:
            assignment = await claim_team(
                db, interaction.guild_id, settings["season"], interaction.user.id,
                team, source="self_claim", require_open=True,
            )
        except OwnershipError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(
            content=(
                f"✅ **{assignment.team_name} claimed.** Your ownership is secured; "
                "I’m applying the team role and matchup updates now."
            )
        )
        errors = await sync_assignment_discord(
            interaction.client, db, interaction.guild, assignment
        )
        await refresh_open_team_card(
            interaction.client,
            db,
            interaction.guild_id,
            assignment.external_team_id or assignment.team_name,
        )
        await db.audit(
            interaction.guild_id, interaction.user.id, "team_self_claimed",
            target_type="team", target_id=assignment.team_name,
            details={"role_sync_errors": errors},
        )
        message = (
            f"You now own **{assignment.team_name}** for the full season. "
            "Your role and all imported matchup assignments were updated."
        )
        if errors:
            message += "\nSome Discord updates need `/syncmemberroles`."
        await interaction.edit_original_response(content=message)

    @tree.command(name="importmembers", description="Preview a season member-to-team CSV import.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Replace current-season ownership", value="replace"),
        app_commands.Choice(name="Merge into current ownership", value="merge"),
    ])
    async def import_members(
        interaction: discord.Interaction,
        csv_file: discord.Attachment,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        selected_mode = mode.value if mode else "replace"
        if csv_file.size > 2 * 1024 * 1024:
            await interaction.response.send_message("Member CSV files must be 2 MB or smaller.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await csv_file.read()
        except discord.HTTPException:
            await interaction.followup.send("Discord could not download that CSV. Try attaching it again.", ephemeral=True)
            return
        valid_teams = await active_team_names(db, interaction.guild_id, settings["season"])
        preview = parse_member_csv(data, interaction.guild, valid_teams)
        preview = await validate_import_conflicts(
            db, interaction.guild_id, settings["season"], preview, selected_mode
        )
        embed = discord.Embed(
            title=f"Member Import Preview — {selected_mode.title()} Mode",
            color=discord.Color.red() if preview.errors else discord.Color.gold(),
        )
        if preview.rows:
            embed.description = "\n".join(
                f"<@{row.user_id}> ? **{row.team_name}**" for row in preview.rows[:32]
            )[:4000]
        if preview.errors:
            embed.add_field(name="Blocking Errors", value="\n".join(preview.errors[:10])[:1024], inline=False)
        if preview.warnings:
            embed.add_field(name="Warnings", value="\n".join(preview.warnings[:8])[:1024], inline=False)
        embed.set_footer(text="Nothing changes until Confirm Member Import is pressed.")
        await interaction.followup.send(
            embed=embed,
            view=(ConfirmMemberImportView(db, preview.rows, selected_mode, interaction.user.id) if not preview.errors else None),
            ephemeral=True,
        )

    @tree.command(name="syncmemberroles", description="Repair team roles from saved ownership mappings.")
    async def sync_member_roles(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        errors = await sync_all_member_roles(interaction.guild, db)
        await db.audit(
            interaction.guild_id, interaction.user.id, "member_roles_synced",
            details={"errors": errors},
        )
        await interaction.followup.send(
            "Team roles synchronized successfully." if not errors else
            "Role sync finished with warnings:\n" + "\n".join(errors[:12]),
            ephemeral=True,
        )
    @tree.command(name="profile", description="View a member's active team and permanent Madden career.")
    async def profile(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        user_id: str | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        if member and user_id:
            await interaction.response.send_message(
                "Choose a member or enter a departed user's ID, not both.", ephemeral=True
            )
            return
        try:
            profile_target_id = (
                member.id if member else int(user_id) if user_id else interaction.user.id
            )
        except ValueError:
            await interaction.response.send_message(
                "Discord user ID must contain only numbers.", ephemeral=True
            )
            return
        target = member or interaction.guild.get_member(profile_target_id)
        display_name = target.display_name if target else f"User {profile_target_id}"
        display_color = target.color if target else discord.Color.blurple()
        row = await db.fetchone(
            "SELECT * FROM profiles WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, profile_target_id),
        )
        career = await db.fetchone(
            "SELECT * FROM career_profiles WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, profile_target_id),
        )
        seasons = await db.fetchall(
            """SELECT * FROM season_participants
               WHERE guild_id=? AND user_id=?
               ORDER BY updated_at DESC LIMIT 6""",
            (interaction.guild_id, profile_target_id),
        )
        if not row and not career and not seasons:
            await interaction.response.send_message(
                "This member has not participated in a Madden season yet.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"{display_name}'s Madden Career",
            color=display_color,
        )
        if row:
            embed.add_field(
                name="Current Team",
                value=f"{row['team_name']} · {'Approved' if row['approved'] else 'Pending'}",
                inline=False,
            )
            if row["twitch"] or row["youtube"]:
                links = [value for value in (row["twitch"], row["youtube"]) if value]
                embed.add_field(name="Streams", value="\n".join(links), inline=False)
        if career:
            level = level_for_xp(career["xp"])
            embed.add_field(
                name=f"Level {level} · {career['xp']} XP",
                value=f"Next level at **{next_level_xp(level)} XP**",
                inline=False,
            )
            embed.add_field(
                name="Career Record",
                value=(
                    f"**{career['wins']}-{career['losses']}** · "
                    f"{win_ratio(career['wins'], career['losses']):.1f}% win rate\n"
                    f"Played: {career['games']} · Force wins: {career['force_wins']} · "
                    f"Forfeits: {career['forfeits']} · Sims: {career['sims']}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Championships",
                value=str(career["championships"]),
            )
        if seasons:
            history = []
            for season_row in seasons:
                crown = " 🏆" if season_row["champion"] else ""
                history.append(
                    f"Season **{season_row['season']}** · {season_row['team_name']}{crown} · "
                    f"{season_row['wins']}-{season_row['losses']} · {season_row['xp']} XP"
                )
            embed.add_field(
                name="Season History",
                value="\n".join(history)[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @tree.command(name="profile-approve", description="Approve or reject a member's team claim.")
    async def profile_approve(
        interaction: discord.Interaction, member: discord.Member, approve: bool = True
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        commissioner = await is_commissioner(interaction, settings)
        await interaction.response.defer(ephemeral=commissioner, thinking=True)

        profile_row = await db.fetchone(
            "SELECT * FROM profiles WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, member.id),
        )
        if not profile_row:
            await interaction.followup.send("That member has no registration.", ephemeral=True)
            return
        if approve:
            try:
                await assign_team_role(db, member, profile_row["team_name"])
            except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(
                    f"I did not approve this claim because the team role could not be assigned: {exc}",
                    ephemeral=True,
                )
                return
        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            if approve:
                cursor = await conn.execute(
                    """SELECT user_id FROM profiles
                       WHERE guild_id=? AND approved=1
                       AND lower(team_name)=lower(?) AND user_id != ?""",
                    (interaction.guild_id, profile_row["team_name"], member.id),
                )
                if await cursor.fetchone():
                    await conn.rollback()
                    await interaction.followup.send(
                        "That team is already assigned to another approved member.",
                        ephemeral=True,
                    )
                    return
            await conn.execute(
                """UPDATE profiles SET approved=?,approved_by=?,assignment_source='commissioner',
                   assigned_at=CASE WHEN ?=1 THEN ? ELSE assigned_at END,updated_at=?
                   WHERE id=?""",
                (
                    int(approve), interaction.user.id, int(approve), iso_now(),
                    iso_now(), profile_row["id"]
                ),
            )
            await conn.commit()
        if approve:
            await db.execute(
                "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND lower(team_name)=lower(?)",
                (interaction.guild_id, settings["season"], profile_row["team_name"]),
            )
            await db.execute(
                """UPDATE matchups SET away_user_id=?, updated_at=?
                   WHERE guild_id=? AND season=? AND lower(away_team)=lower(?)
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (
                    member.id, iso_now(), interaction.guild_id, settings["season"],
                    profile_row["team_name"],
                ),
            )
            await db.execute(
                """UPDATE matchups SET home_user_id=?, updated_at=?
                   WHERE guild_id=? AND season=? AND lower(home_team)=lower(?)
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (
                    member.id, iso_now(), interaction.guild_id, settings["season"],
                    profile_row["team_name"],
                ),
            )
            affected = await db.fetchall(
                """SELECT id FROM matchups WHERE guild_id=? AND season=?
                   AND (lower(away_team)=lower(?) OR lower(home_team)=lower(?))
                   AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
                (
                    interaction.guild_id, settings["season"],
                    profile_row["team_name"], profile_row["team_name"],
                ),
            )
            for affected_matchup in affected:
                await refresh_matchup_message(
                    interaction.client, db, affected_matchup["id"]
                )
            async with db.connect() as conn:
                await ensure_participant(
                    conn,
                    guild_id=interaction.guild_id,
                    season=settings["season"],
                    user_id=member.id,
                    team_name=profile_row["team_name"],
                )
                await conn.commit()
        else:
            await db.execute(
                """UPDATE matchups SET away_user_id=NULL, updated_at=?
                   WHERE guild_id=? AND season=? AND away_user_id=?""",
                (iso_now(), interaction.guild_id, settings["season"], member.id),
            )
            await db.execute(
                """UPDATE matchups SET home_user_id=NULL, updated_at=?
                   WHERE guild_id=? AND season=? AND home_user_id=?""",
                (iso_now(), interaction.guild_id, settings["season"], member.id),
            )
            try:
                await remove_team_role(db, member, profile_row["team_name"])
            except (discord.Forbidden, discord.HTTPException):
                pass
        await db.audit(
            interaction.guild_id, interaction.user.id,
            "profile_approved" if approve else "profile_rejected",
            target_type="user", target_id=str(member.id),
        )
        await refresh_open_team_card(
            interaction.client, db, interaction.guild_id, profile_row["team_name"]
        )
        result = f"{member.mention}'s claim was {'approved' if approve else 'rejected'}."
        if approve:
            try:
                await member.send(
                    f"Your request to own **{profile_row['team_name']}** for Season "
                    f"**{settings['season']}** was approved."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(result)

    @tree.command(name="team-release", description="Safely release an owner from their current Madden team.")
    async def team_release(
        interaction: discord.Interaction,
        reason: str,
        member: discord.Member | None = None,
        user_id: str | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        if len(reason.strip()) < 10:
            await interaction.response.send_message(
                "Provide a clear reason of at least 10 characters.", ephemeral=True
            )
            return
        if member and user_id:
            await interaction.response.send_message(
                "Choose a member or enter a departed user's ID, not both.", ephemeral=True
            )
            return
        try:
            target_id = member.id if member else int((user_id or "").strip())
        except ValueError:
            target_id = 0
        if not target_id:
            await interaction.response.send_message(
                "Choose the current member, or provide the Discord user ID shown in the departure alert.",
                ephemeral=True,
            )
            return
        profile_row = await db.fetchone(
            """SELECT * FROM profiles
               WHERE guild_id=? AND user_id=? AND approved=1""",
            (interaction.guild_id, target_id),
        )
        if not profile_row:
            await interaction.response.send_message(
                "That user does not currently own an approved team.", ephemeral=True
            )
            return
        active = await db.fetchone(
            """SELECT COUNT(*) AS total FROM matchups
               WHERE guild_id=? AND season=?
               AND (away_user_id=? OR home_user_id=?)
               AND status NOT IN ('complete','force_home','force_away','fair_sim')""",
            (
                interaction.guild_id,
                settings["season"],
                target_id,
                target_id,
            ),
        )
        owner_display = member.mention if member else f"<@{target_id}> (`{target_id}`)"
        embed = discord.Embed(
            title="Release Team Owner",
            description=(
                f"**Owner:** {owner_display}\n"
                f"**Team:** {profile_row['team_name']}\n"
                f"**Unfinished matchups affected:** {active['total']}\n"
                f"**Reason:** {reason.strip()}\n\n"
                "Permanent career and season ownership history will remain. "
                "Unfinished games will return to Waiting and the team will become available."
            ),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=TeamReleaseConfirmationView(
                db,
                guild_id=interaction.guild_id,
                season=settings["season"],
                user_id=target_id,
                team_name=profile_row["team_name"],
                reason=reason.strip(),
                actor_id=interaction.user.id,
            ),
            ephemeral=True,
        )
    @tree.command(name="importrosters", description="Import the season team and roster data CSV.")
    async def import_rosters(
        interaction: discord.Interaction, csv_file: discord.Attachment
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        if not csv_file.filename.lower().endswith(".csv"):
            await interaction.response.send_message("Attach a `.csv` file.", ephemeral=True)
            return
        if csv_file.size > 8 * 1024 * 1024:
            await interaction.response.send_message("Roster CSV files must be 8 MB or smaller.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows, errors = parse_roster_import(await csv_file.read())
        summary = roster_team_summary(rows)
        if rows and len(summary) != 32:
            errors.append(f"Expected 32 teams, but this file contains {len(summary)}.")
        if errors:
            await interaction.followup.send(
                "**Roster import validation failed:**\n" + "\n".join(f"• {item}" for item in errors[:20]),
                ephemeral=True,
            )
            return
        preview = "\n".join(
            f"**{name}** (`{abbr}`) — {count} players"
            for _, name, abbr, count in summary
        )
        embed = discord.Embed(
            title="Season Roster Import Preview",
            description=(
                f"Season **{settings['season']}** · **{len(summary)} teams** · "
                f"**{len(rows)} players**\n\n{preview}"
            )[:4096],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Confirming replaces only this season's roster snapshot. Ownership and completed history remain.")
        await interaction.followup.send(
            embed=embed,
            view=ConfirmRosterImportView(db, rows, interaction.user.id),
            ephemeral=True,
        )

    @tree.command(name="importfixtures", description="Import the complete season fixture CSV and optionally start Week 1.")
    async def import_fixtures(
        interaction: discord.Interaction,
        csv_file: discord.Attachment,
        start_now: bool = True,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        if not csv_file.filename.lower().endswith(".csv"):
            await interaction.response.send_message("Attach a `.csv` file.", ephemeral=True)
            return
        if csv_file.size > 5 * 1024 * 1024:
            await interaction.response.send_message("Fixture CSV files must be 5 MB or smaller.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows, errors = parse_fixture_import(await csv_file.read())
        weeks = sorted({row.week for row in rows})
        teams = {row.away_team_id for row in rows} | {row.home_team_id for row in rows}
        if rows and len(teams) != 32:
            errors.append(f"Expected fixtures for 32 teams, but found {len(teams)} team IDs.")
        if errors:
            await interaction.followup.send(
                "**Fixture import validation failed:**\n" + "\n".join(f"• {item}" for item in errors[:20]),
                ephemeral=True,
            )
            return
        roster_count = await db.fetchone(
            "SELECT COUNT(*) AS total FROM franchises WHERE guild_id=? AND season=?",
            (interaction.guild_id, settings["season"]),
        )
        if not roster_count or roster_count["total"] != 32:
            await interaction.followup.send(
                "Import the 32-team roster file with `/importrosters` first.", ephemeral=True
            )
            return
        per_week = []
        for week in weeks:
            per_week.append(f"Week {week}: {sum(row.week == week for row in rows)} games")
        embed = discord.Embed(
            title="Season Fixture Import Preview",
            description=(
                f"Season **{settings['season']}** · **{len(rows)} games** · "
                f"**{len(weeks)} weeks** · **{len(teams)} teams**\n\n"
                + "\n".join(per_week)
            )[:4096],
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Season clock",
            value=(
                "Starts immediately. Only Week 1 channels are created; the bot opens each next week every 7 days."
                if start_now else
                "Fixtures are stored without changing the active week or creating channels."
            ),
            inline=False,
        )
        embed.set_footer(text="Old weekly channels are removed on rollover; fixtures, results, records, and ownership stay saved.")
        await interaction.followup.send(
            embed=embed,
            view=ConfirmFixtureImportView(db, rows, interaction.user.id, start_now),
            ephemeral=True,
        )

    async def imported_team_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        settings = await db.settings(interaction.guild_id)
        rows = await db.fetchall(
            """SELECT team_name,abbreviation FROM franchises
               WHERE guild_id=? AND season=? ORDER BY team_name""",
            (interaction.guild_id, settings["season"]),
        )
        query = normalize_team_name(current)
        choices = []
        for row in rows:
            searchable = normalize_team_name(
                f"{row['team_name']} {row['abbreviation'] or ''}"
            )
            if query and query not in searchable:
                continue
            label = (
                f"{row['team_name']} ({row['abbreviation']})"
                if row["abbreviation"] else row["team_name"]
            )
            choices.append(
                app_commands.Choice(name=label[:100], value=row["team_name"])
            )
        return choices[:25]
    @tree.command(name="syncteamemojis", description="Match imported teams to this server’s NFL emoji names.")
    async def sync_team_emoji_command(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        matched, missing = await sync_team_emojis(db, interaction.guild, settings["season"])
        await refresh_team_emoji_posts(bot, db, interaction.guild, settings["season"])
        detail = f"Matched **{matched}** franchise emoji(s) by name and refreshed team posts."
        if missing:
            detail += "\nNo emoji found for: " + ", ".join(missing)
        await interaction.edit_original_response(content=detail[:1900])

    @tree.command(name="setteamemoji", description="Override one team’s custom emoji name.")
    @app_commands.autocomplete(team=imported_team_autocomplete)
    async def set_team_emoji_command(
        interaction: discord.Interaction, team: str, emoji_name: str
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        resolved = emoji_named(interaction.guild, emoji_name)
        if resolved is None:
            await interaction.response.send_message(
                "No server custom emoji has that name. Enter its name without colons.",
                ephemeral=True,
            )
            return
        cursor = await db.execute(
            """UPDATE franchises SET emoji_name=? WHERE guild_id=? AND season=?
               AND lower(team_name)=lower(?)""",
            (resolved.name, interaction.guild_id, settings["season"], team),
        )
        await db.audit(
            interaction.guild_id, interaction.user.id, "team_emoji_updated",
            target_type="team", target_id=team, details={"emoji_name": resolved.name},
        )
        await refresh_team_emoji_posts(bot, db, interaction.guild, settings["season"])
        await interaction.response.send_message(
            f"{resolved} **{team}** now uses `:{resolved.name}:`.", ephemeral=True
        )

    async def game_matchup_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if interaction.guild_id is None:
            return []
        settings = await db.settings(interaction.guild_id)
        rows = await db.fetchall(
            """SELECT id,week,away_team,home_team FROM matchups
               WHERE guild_id=? AND season=? ORDER BY week,id""",
            (interaction.guild_id, settings["season"]),
        )
        query = current.casefold().strip()
        choices = []
        for row in rows:
            label = f"W{row['week']} · {row['away_team']} at {row['home_team']}"
            if query and query not in label.casefold() and query != str(row["id"]):
                continue
            choices.append(app_commands.Choice(name=label[:100], value=row["id"]))
        return choices[:25]
    @tree.command(name="roster", description="View the imported season roster for a team.")
    @app_commands.autocomplete(team=imported_team_autocomplete)
    async def roster(interaction: discord.Interaction, team: str) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        franchise = await db.fetchone(
            """SELECT * FROM franchises WHERE guild_id=? AND season=?
               AND (lower(team_name)=lower(?) OR lower(abbreviation)=lower(?))""",
            (interaction.guild_id, settings["season"], team.strip(), team.strip()),
        )
        if not franchise:
            names = await active_team_names(db, interaction.guild_id, settings["season"])
            await interaction.response.send_message(
                "Unknown team. Imported teams: " + ", ".join(names), ephemeral=True
            )
            return
        players = await db.fetchall(
            """SELECT * FROM roster_players WHERE guild_id=? AND season=? AND external_team_id=?
               ORDER BY position, overall DESC, full_name""",
            (interaction.guild_id, settings["season"], franchise["external_team_id"]),
        )
        lines = [
            f"`{row['position']:<3}` **{row['full_name']}** · OVR {row['overall'] if row['overall'] is not None else '—'}"
            for row in players
        ]
        description = "\n".join(lines)
        if len(description) > 3900:
            description = description[:3880].rsplit("\n", 1)[0] + "\n…"
        embed = discord.Embed(
            title=f"{franchise['team_name']} Roster",
            description=description or "No roster players were imported.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(players)} players · Season {settings['season']} · imported data snapshot")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="playersearch", description="Search imported season roster players.")
    async def player_search(interaction: discord.Interaction, player: str) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        rows = await db.fetchall(
            """SELECT * FROM roster_players WHERE guild_id=? AND season=?
               AND lower(full_name) LIKE lower(?) ORDER BY overall DESC,full_name LIMIT 10""",
            (interaction.guild_id, settings["season"], f"%{player.strip()}%"),
        )
        if not rows:
            await interaction.response.send_message("No imported player matched that search.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Player Search: {player}", color=discord.Color.blurple())
        for row in rows:
            status = "IR" if row["is_on_ir"] else ("Practice squad" if row["is_practice_squad"] else "Active")
            embed.add_field(
                name=f"{row['full_name']} · {row['position']} · {row['team_name']}",
                value=(
                    f"OVR **{row['overall'] if row['overall'] is not None else '—'}** · "
                    f"Age {row['age'] if row['age'] is not None else '—'} · "
                    f"#{row['jersey_number'] or '—'} · {status}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)
    @tree.command(name="import-schedule", description="Validate and preview a league schedule CSV.")
    async def import_schedule(interaction: discord.Interaction, csv_file: discord.Attachment) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        if not csv_file.filename.lower().endswith(".csv"):
            await interaction.response.send_message("Attach a `.csv` file.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        games, errors = parse_schedule_csv(await csv_file.read())
        official = {team.casefold(): team for team in await active_team_names(db, interaction.guild_id, settings["season"])}
        invalid = sorted({
            team for game in games for team in (game.away_team, game.home_team)
            if team.casefold() not in official
        }, key=str.casefold)
        if invalid:
            errors.append(
                "Unknown Madden team(s): " + ", ".join(invalid)
                + ". Use the exact NFL nickname; letter case does not matter."
            )
        if not errors:
            games = [
                type(game)(
                    game.week, official[game.away_team.casefold()],
                    official[game.home_team.casefold()], game.away_user_id,
                    game.home_user_id, game.external_key,
                )
                for game in games
            ]
        if errors:
            await interaction.followup.send(
                "**Import validation failed:**\n" + "\n".join(f"• {item}" for item in errors[:15]),
                ephemeral=True,
            )
            return
        if not games:
            await interaction.followup.send("The file contains no games.", ephemeral=True)
            return
        weeks = sorted({game.week for game in games})
        if len(weeks) != 1:
            await interaction.followup.send(
                "Import exactly one week per CSV. This file contains weeks "
                + ", ".join(str(week) for week in weeks) + ".",
                ephemeral=True,
            )
            return
        if weeks[0] != settings["current_week"]:
            await interaction.followup.send(
                f"This league is currently on Week {settings['current_week']}. "
                f"Upload that week's CSV first (this file is Week {weeks[0]}).",
                ephemeral=True,
            )
            return
        deadline = next_deadline(
            settings["timezone"], settings["advance_weekday"], settings["advance_time"]
        ).isoformat()
        preview = "\n".join(
            f"Week {game.week}: {game.away_team} @ {game.home_team}" for game in games[:20]
        )
        if len(games) > 20:
            preview += f"\n…and {len(games) - 20} more"
        await interaction.followup.send(
            f"**Import preview ({len(games)} games)**\n{preview}\n\n"
            "No matchup records or channels have been created yet.",
            view=ConfirmImportView(db, games, settings["season"], deadline, interaction.user.id),
            ephemeral=True,
        )

    @tree.command(name="createweek", description="Create missing matchup channels in that week's category.")
    async def create_week(
        interaction: discord.Interaction, number: int | None = None
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        selected = number or settings["current_week"]
        rows = await db.fetchone(
            """SELECT COUNT(*) AS total FROM matchups
               WHERE guild_id=? AND season=? AND week=?""",
            (interaction.guild_id, settings["season"], selected),
        )
        if not rows or not rows["total"]:
            await interaction.response.send_message(
                f"No schedule is imported for Week {selected}.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with bot.week_lock(interaction.guild_id, settings["season"], selected):
            created, errors = await create_week_matchup_channels(
                interaction, db, season=settings["season"], week=selected
            )
        category_row = await db.fetchone(
            "SELECT category_id FROM week_categories WHERE guild_id=? AND season=? AND week=?",
            (interaction.guild_id, settings["season"], selected),
        )
        category = interaction.guild.get_channel(category_row["category_id"] or 0) if category_row else None
        destination = f"**{category.name}**" if isinstance(category, discord.CategoryChannel) else f"the Week {selected} category"
        message = f"Week {selected} is ready under {destination}. Created **{created}** channel(s)."
        if errors:
            message += "\n⚠️ " + "\n".join(errors[:5])
        await interaction.followup.send(message, ephemeral=True)
    @tree.command(name="week", description="Show the week dashboard and create missing matchup channels.")
    @app_commands.describe(
        action="Legacy action; Commissioners should normally use the dashboard buttons",
        matchup_id="Legacy action target; select a matchup from the dashboard instead",
    )
    @app_commands.autocomplete(action=week_action_autocomplete)
    async def week(
        interaction: discord.Interaction, number: int | None = None,
        action: str | None = None, matchup_id: int | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        selected = number or settings["current_week"]
        if action:
            if not await require_commissioner(interaction, settings):
                return
            if action == "advance":
                unresolved = await db.fetchone(
                    """SELECT COUNT(*) AS total FROM matchups WHERE guild_id=? AND season=?
                       AND week=? AND status NOT IN
                       ('complete','force_home','force_away','fair_sim')""",
                    (interaction.guild_id, settings["season"], selected),
                )

                await interaction.response.send_message(
                    f"Advance from **Week {selected}** with **{unresolved['total']} unresolved** game(s)?",
                    view=AdvanceWeekConfirmationView(
                        db,
                        interaction.guild_id,
                        settings["season"],
                        selected,
                        interaction.user.id,
                    ),
                    ephemeral=True,
                )
                return
            if action not in FINAL_STATUSES or not matchup_id:
                await interaction.response.send_message(
                    "Choose an action from the list and provide its matchup ID, or use the interactive dashboard.",
                    ephemeral=True,
                )
                return
            target = await db.fetchone(
                """SELECT * FROM matchups
                   WHERE id=? AND guild_id=? AND season=? AND week=?""",
                (matchup_id, interaction.guild_id, settings["season"], selected),
            )
            if not target:
                await interaction.response.send_message(
                    "Matchup not found in this week.", ephemeral=True
                )
                return
            if action == "complete":
                if (
                    target["status"] not in ("result_pending", "issue_reported")
                    or not target["result_submitted_by"]
                    or not target["result_evidence_url"]
                ):
                    await interaction.response.send_message(
                        "That matchup has no complete score-and-screenshot submission to approve.",
                        ephemeral=True,
                    )
                    return
                view = CommissionerResultConfirmationView(
                    db, matchup_id, "approve", interaction.user.id,
                    target["result_submission_version"]
                )
                description = "approve the submitted score"
            else:
                view = OutcomeConfirmationView(
                    db, matchup_id, action, interaction.user.id
                )
                description = action.replace("_", " ")
            await interaction.response.send_message(
                f"Confirm **{description}** for matchup #{matchup_id}.",
                view=view,
                ephemeral=True,
            )
            return
        rows = await db.fetchall(
            """SELECT * FROM matchups WHERE guild_id=? AND season=? AND week=? ORDER BY id""",
            (interaction.guild_id, settings["season"], selected),
        )
        if not rows:
            await interaction.response.send_message("No games are imported for that week.", ephemeral=True)
            return
        commissioner = await is_commissioner(interaction, settings)
        await interaction.response.defer(ephemeral=commissioner, thinking=True)
        missing_channel = any(
            not isinstance(interaction.guild.get_channel(row["channel_id"] or 0), discord.TextChannel)
            for row in rows
        )
        if commissioner and selected == settings["current_week"] and missing_channel:
            async with bot.week_lock(interaction.guild_id, settings["season"], selected):
                await create_week_matchup_channels(
                    interaction, db, season=settings["season"], week=selected
                )
                rows = await db.fetchall(
                    """SELECT * FROM matchups WHERE guild_id=? AND season=? AND week=?
                       ORDER BY id""",
                    (interaction.guild_id, settings["season"], selected),
                )
        lines = []
        for row in rows:
            live_channel = interaction.guild.get_channel(row["channel_id"] or 0)
            if isinstance(live_channel, discord.TextChannel):
                link = live_channel.mention
            elif selected < settings["current_week"]:
                link = "Archived · weekly channel removed · Commissioner decisions remain available"
            else:
                link = "Channel not created — ask a Commissioner to run `/week`"
            lines.append(
                f"`#{row['id']}` {row['away_team']} @ {row['home_team']} — "
                f"**{status_label(row['status'], row['deadline_at'])}** · {link}"
            )
        embed = discord.Embed(
            title=f"{settings['league_name']} · Week {selected}",
            description="\n".join(lines)[:4000],
            color=0x2B2D31,
        )
        view = (
            CommissionerWeekDashboardView(
                db,
                rows,
                guild_id=interaction.guild_id,
                season=settings["season"],
                week=selected,
                actor_id=interaction.user.id,
                can_advance=selected == settings['current_week'],
            )
            if commissioner
            else None
        )
        embed.set_footer(
            text=(
                "Select a matchup below to open Commissioner controls."
                if commissioner
                else "Players should use the reactions on their matchup card."
            )
        )
        await send_week_dashboard(interaction.followup, embed, view)

    @tree.command(name="aistatus", description="Show Gemini AI readiness and today's usage.")
    async def ai_status(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        from datetime import UTC, datetime
        usage = await db.fetchone(
            "SELECT requests FROM ai_daily_usage WHERE usage_date=? AND guild_id=?",
            (datetime.now(UTC).date().isoformat(), interaction.guild_id),
        )
        ready = bot.ai.available and bool(settings.get("ai_enabled", 1))
        embed = discord.Embed(
            title="Gemini AI Status",
            color=discord.Color.green() if ready else discord.Color.red(),
        )
        embed.add_field(name="Status", value="Ready" if ready else "Disabled or unavailable")
        embed.add_field(name="Model", value=bot.config.gemini_model or "Not configured")
        embed.add_field(name="Today", value=f"{usage['requests'] if usage else 0}/{bot.config.ai_daily_limit} requests")
        embed.add_field(name="Style", value=settings.get("ai_style") or "Default", inline=False)
        embed.set_footer(text="The API key is never displayed or sent to Discord.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="aisettings", description="Enable/disable AI and choose its writing style.")
    async def ai_settings(
        interaction: discord.Interaction,
        enabled: bool | None = None,
        style: str | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        updates = {}
        if enabled is not None:
            updates["ai_enabled"] = int(enabled)
        if style is not None:
            clean = " ".join(style.split())[:120]
            if not clean:
                await interaction.response.send_message("AI style cannot be empty.", ephemeral=True)
                return
            updates["ai_style"] = clean
        if updates:
            await db.update_settings(interaction.guild_id, **updates)
            await db.audit(interaction.guild_id, interaction.user.id, "ai_settings_updated", details=updates)
        current = await db.settings(interaction.guild_id)
        await interaction.response.send_message(
            f"AI is **{'enabled' if current['ai_enabled'] else 'disabled'}**. Style: **{current['ai_style']}**.",
            ephemeral=True,
        )

    @tree.command(name="airecap", description="Generate the idempotent AI recap for a scored final.")
    async def ai_recap(interaction: discord.Interaction, matchup_id: int) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        matchup = await db.fetchone("SELECT guild_id FROM matchups WHERE id=?", (matchup_id,))
        if not matchup or matchup["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("That matchup was not found in this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await bot.ai.matchup_recap(matchup_id)
        await interaction.followup.send(("Recap posted." if ok else detail)[:1900], ephemeral=True)

    @tree.command(name="aiweek", description="Post deterministic power rankings and AI weekly storylines.")
    async def ai_week(interaction: discord.Interaction, week: int | None = None) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        selected = week if week is not None else max(1, settings["current_week"] - 1)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await bot.ai.weekly_content(
            interaction.guild_id, settings["season"], selected
        )
        await interaction.followup.send(("Weekly spotlight posted." if ok else detail)[:1900], ephemeral=True)

    @tree.command(name="gameoftheweek", description="Post the weekly matchup graphic and prediction vote.")
    @app_commands.autocomplete(matchup_id=game_matchup_autocomplete)
    async def game_of_the_week(
        interaction: discord.Interaction,
        week: int,
        matchup_id: int,
        graphic: discord.Attachment,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        matchup = await db.fetchone(
            """SELECT * FROM matchups WHERE id=? AND guild_id=? AND season=?""",
            (matchup_id, interaction.guild_id, settings["season"]),
        )
        if not matchup or matchup["week"] != week:
            await interaction.response.send_message(
                "Choose a matchup from the selected week.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await post_game_of_week(
            interaction.client, db, guild=interaction.guild,
            season=settings["season"], week=week, matchup=matchup,
            graphic=graphic, actor_id=interaction.user.id,
        )
        await interaction.edit_original_response(content=detail[:1900])
    async def save_week_mvp(
        interaction: discord.Interaction,
        week: int,
        player: str,
        team: str,
        stats: str,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        valid_teams = await active_team_names(db, interaction.guild_id, settings["season"])
        if normalize_team_name(team) not in {normalize_team_name(item) for item in valid_teams}:
            await interaction.response.send_message(
                "Choose one of the imported Madden team names.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await upsert_week_mvp(
            interaction.client, db, interaction.guild_id, settings["season"],
            week, player.strip()[:100], team, stats.strip()[:1000], interaction.user.id,
        )
        await interaction.edit_original_response(
            content=f"Week {week} MVP saved. The Weekly Recap was updated if it already exists."
        )

    @tree.command(name="weekmvp", description="Set the MVP performance used in a Weekly Recap.")
    @app_commands.autocomplete(team=imported_team_autocomplete)
    async def week_mvp(
        interaction: discord.Interaction, week: int, player: str, team: str, stats: str
    ) -> None:
        await save_week_mvp(interaction, week, player, team, stats)

    @tree.command(name="aipotw", description="Alias for /weekmvp.")
    @app_commands.autocomplete(team=imported_team_autocomplete)
    async def ai_potw(
        interaction: discord.Interaction, week: int, player: str, team: str, stats: str
    ) -> None:
        await save_week_mvp(interaction, week, player, team, stats)
    @tree.command(name="announce", description="Post an official league announcement.")
    async def announce(interaction: discord.Interaction, message: str) -> None:
        await post_staff_message(
            interaction, db, "announcements", "announcements_channel_id",
            "📣 League Announcement", message,
        )

    @tree.command(name="award", description="Post a Game or Player of the Week award.")
    async def award(interaction: discord.Interaction, title: str, recipient: str, details: str) -> None:
        await post_staff_message(
            interaction, db, "awards", "storyline_channel_id",
            f"🏆 {title}", f"**{recipient}**\n{details}"
        )

    @tree.command(name="seasonaward", description="Set or override one end-of-season award.")
    @app_commands.choices(category=[
        app_commands.Choice(name=label, value=key)
        for key, label in AWARD_CATEGORIES
    ])
    async def season_award(
        interaction: discord.Interaction,
        category: str,
        recipient: str,
        details: str,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await set_season_award(
            db, interaction.guild_id, settings["season"], category,
            recipient, details, interaction.user.id,
        )
        await interaction.response.send_message(
            "Season award saved. Run `/seasonawards` to review the full draft.",
            ephemeral=True,
        )

    @tree.command(name="seasonawards", description="Review and publish the complete season awards summary.")
    async def season_awards(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ensure_award_suggestions(
            db, interaction.guild_id, settings["season"], interaction.user.id
        )
        embed = await season_awards_embed(db, interaction.guild, settings["season"])
        await interaction.edit_original_response(
            embed=embed,
            view=SeasonAwardsApprovalView(
                db, interaction.guild_id, settings["season"], interaction.user.id
            ),
        )
    @tree.command(name="trade-block", description="Add a player to your league trade block.")
    async def trade_block(interaction: discord.Interaction, player: str, notes: str | None = None) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not settings["features"].get("trades"):
            await interaction.response.send_message("Trade tracking is disabled here.", ephemeral=True)
            return
        await db.execute(
            """INSERT INTO trade_block
               (guild_id,season,user_id,player_name,notes,created_at) VALUES (?,?,?,?,?,?)""",
            (interaction.guild_id, settings["season"], interaction.user.id, player, notes, iso_now()),
        )
        channel = interaction.guild.get_channel(settings.get("trade_channel_id") or 0)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="🔄 Trade Block Update",
                description=f"**{player}** was listed by {interaction.user.mention}.",
                color=discord.Color.gold(),
            )
            if notes:
                embed.add_field(name="Notes", value=notes[:1024], inline=False)
            await channel.send(embed=embed)
        await interaction.response.send_message(
            f"**{player}** was added to your trade block"
            + (f" and posted in {channel.mention}." if isinstance(channel, discord.TextChannel) else ". Configure `/settradechannel` to post it automatically."),
            ephemeral=True,
        )

    @tree.command(name="transaction", description="Record a trade or college transfer.")
    async def transaction(interaction: discord.Interaction, kind: str, details: str) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        allowed_feature = "transfers" if "transfer" in kind.lower() else "trades"
        if not settings["features"].get(allowed_feature):
            await interaction.response.send_message("That module is disabled here.", ephemeral=True)
            return
        await db.execute(
            """INSERT INTO transactions
               (guild_id,season,kind,user_id,details,created_at) VALUES (?,?,?,?,?,?)""",
            (interaction.guild_id, settings["season"], kind, interaction.user.id, details, iso_now()),
        )
        destination_field = (
            "transactions_channel_id" if allowed_feature == "transfers"
            else "trade_channel_id"
        )
        channel = interaction.guild.get_channel(
            settings.get(destination_field) or settings.get("transactions_channel_id") or 0
        )
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"**{kind}** · {interaction.user.mention}\n{details}")
        await interaction.response.send_message("Transaction recorded.", ephemeral=True)

    @tree.command(name="open-roster", description="Add or remove a team from Open Rosters/Open Teams.")
    @app_commands.autocomplete(team=imported_team_autocomplete)
    async def open_roster(
        interaction: discord.Interaction, team: str, is_open: bool, notes: str | None = None
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if is_open:
            await db.execute(
                """INSERT INTO open_rosters (guild_id,season,team_name,notes,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(guild_id,season,team_name)
                   DO UPDATE SET notes=excluded.notes,updated_at=excluded.updated_at""",
                (interaction.guild_id, settings["season"], team, notes, iso_now()),
            )
        else:
            await db.execute(
                "DELETE FROM open_rosters WHERE guild_id=? AND season=? AND team_name=?",
                (interaction.guild_id, settings["season"], team),
            )
        await db.audit(
            interaction.guild_id, interaction.user.id,
            "open_roster_add" if is_open else "open_roster_remove",
            target_type="team", target_id=team,
        )
        destination = interaction.guild.get_channel(settings.get("open_teams_channel_id") or 0)
        if isinstance(destination, discord.TextChannel):
            await destination.send(
                embed=discord.Embed(
                    title="📋 Open Teams Update",
                    description=(
                        f"**{team}** is now available." if is_open
                        else f"**{team}** is no longer listed as open."
                    ) + (f"\n{notes}" if notes else ""),
                    color=discord.Color.green() if is_open else discord.Color.red(),
                )
            )
        await refresh_open_team_card(
            interaction.client, db, interaction.guild_id, team
        )
        await interaction.edit_original_response(
            content=(
                f"**{team}** is now {'open' if is_open else 'removed from open teams'}."
                + (f" Posted in {destination.mention}." if isinstance(destination, discord.TextChannel) else "")
            )
        )

    @tree.command(name="season-close", description="Archive this Madden season and start a clean one.")
    async def season_close(
        interaction: discord.Interaction,
        new_season: str,
        champion: discord.Member | None = None,
        champion_user_id: str | None = None,
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        if not await awards_ready(db, interaction.guild_id, settings["season"]):
            await interaction.response.send_message(
                "Complete and publish all eight awards with `/seasonawards` before closing the season.",
                ephemeral=True,
            )
            return
        clean_new = " ".join(new_season.split())
        if champion and champion_user_id:
            await interaction.response.send_message(
                "Choose either champion or champion_user_id, not both.", ephemeral=True
            )
            return
        selected_champion_id = champion.id if champion else None
        if champion_user_id:
            try:
                selected_champion_id = int(champion_user_id.strip())
                if selected_champion_id <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "champion_user_id must be a valid positive Discord user ID.",
                    ephemeral=True,
                )
                return
        if not clean_new or clean_new.casefold() == settings["season"].casefold():
            await interaction.response.send_message(
                "Enter a different, non-empty name for the new season.", ephemeral=True
            )
            return
        preview = await season_close_preview(
            db, interaction.guild_id, settings["season"]
        )
        embed = season_close_embed(
            preview, new_season=clean_new, champion=champion or champion_user_id
        )
        view = (
            SeasonCloseConfirmationView(
                db,
                guild_id=interaction.guild_id,
                season=settings["season"],
                new_season=clean_new,
                actor_id=interaction.user.id,
                champion_user_id=selected_champion_id,
            )
            if preview.can_close
            else None
        )
        if view:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="season-cleanup", description="Retry cleanup for an archived Madden season.")
    async def season_cleanup(
        interaction: discord.Interaction, season: str
    ) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        archive = await db.fetchone(
            "SELECT * FROM season_archives WHERE guild_id=? AND season=?",
            (interaction.guild_id, season),
        )
        if not archive:
            await interaction.response.send_message(
                "That archived season was not found.", ephemeral=True
            )
            return
        if archive["cleanup_status"] == "complete":
            await interaction.response.send_message(
                "That season's Discord cleanup is already complete.", ephemeral=True
            )
            return
        detail = archive["cleanup_error"] or "Cleanup did not finish previously."
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"Retry Season {season} Cleanup",
                description=(
                    f"Current state: **{archive['cleanup_status']}**\n{detail}\n\n"
                    "This deletes only the archived season's old category and remaining channels."
                ),
                color=discord.Color.gold(),
            ),
            view=SeasonCleanupRetryView(
                db,
                guild_id=interaction.guild_id,
                season=season,
                actor_id=interaction.user.id,
            ),
            ephemeral=True,
        )
    @tree.command(name="season-history", description="View archived Madden seasons and official results.")
    async def season_history(
        interaction: discord.Interaction, season: str | None = None
    ) -> None:
        if await deny_dm(interaction):
            return
        if season:
            archive = await db.fetchone(
                "SELECT * FROM season_archives WHERE guild_id=? AND season=?",
                (interaction.guild_id, season),
            )
            if not archive:
                await interaction.response.send_message(
                    "That archived season was not found.", ephemeral=True
                )
                return
            games = await db.fetchall(
                """SELECT * FROM game_history WHERE guild_id=? AND season=?
                   ORDER BY week,id""",
                (interaction.guild_id, season),
            )
            lines = []
            for game in games[:25]:
                score = (
                    f"{game['away_score']}-{game['home_score']}"
                    if game["away_score"] is not None
                    else game["decision_type"].replace("_", " ").title()
                )
                lines.append(
                    f"W{game['week']} · {game['away_team']} @ {game['home_team']} · **{score}**"
                )
            embed = discord.Embed(
                title=f"Madden Season {season} Archive",
                description="\n".join(lines)[:4000] or "No archived games.",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Champion",
                value=(
                    f"<@{archive['champion_user_id']}> · {archive['champion_team']}"
                    if archive["champion_user_id"]
                    else "Not recorded"
                ),
                inline=False,
            )
            embed.set_footer(
                text=f"Discord cleanup: {archive['cleanup_status'].replace('_', ' ').title()}"
            )
        else:
            archives = await db.fetchall(
                """SELECT * FROM season_archives WHERE guild_id=?
                   ORDER BY archived_at DESC LIMIT 20""",
                (interaction.guild_id,),
            )
            if not archives:
                await interaction.response.send_message(
                    "No Madden seasons have been archived yet.", ephemeral=True
                )
                return
            embed = discord.Embed(
                title="Madden Season History",
                description="\n".join(
                    f"**Season {row['season']}** · {row['total_games']} games · "
                    + (f"Champion: {row['champion_team']}" if row["champion_team"] else "No champion recorded")
                    for row in archives
                )[:4000],
                color=discord.Color.blurple(),
            )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="leaderboard", description="View permanent Madden career XP and records.")
    async def leaderboard(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        rows = await db.fetchall(
            """SELECT * FROM career_profiles WHERE guild_id=?
               ORDER BY xp DESC,wins DESC LIMIT 15""",
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.response.send_message(
                "No official Madden results have been recorded yet.", ephemeral=True
            )
            return
        lines = []
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"**{index}.** <@{row['user_id']}> · Level {level_for_xp(row['xp'])} · "
                f"{row['xp']} XP · {row['wins']}-{row['losses']}"
            )
        embed = discord.Embed(
            title="Madden Career Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Force wins and forfeits are tracked separately on /profile.")
        await interaction.response.send_message(embed=embed)
    @tree.command(name="backup", description="Create a portable database backup.")
    async def backup(interaction: discord.Interaction) -> None:
        if await deny_dm(interaction):
            return
        settings = await db.settings(interaction.guild_id)
        if not await require_commissioner(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        path = await make_backup(db, bot.config.backup_dir)
        await db.audit(interaction.guild_id, interaction.user.id, "backup")
        await interaction.followup.send(
            "Backup created. Download and store it securely.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )


async def matchup_embed(db: Database, matchup, settings: dict) -> discord.Embed:
    away_profile = await db.fetchone(
        "SELECT * FROM profiles WHERE guild_id=? AND lower(team_name)=lower(?) AND approved=1",
        (matchup["guild_id"], matchup["away_team"]),
    )
    home_profile = await db.fetchone(
        "SELECT * FROM profiles WHERE guild_id=? AND lower(team_name)=lower(?) AND approved=1",
        (matchup["guild_id"], matchup["home_team"]),
    )
    away_team = await db.fetchone(
        "SELECT * FROM teams WHERE guild_id=? AND season=? AND lower(name)=lower(?)",
        (matchup["guild_id"], matchup["season"], matchup["away_team"]),
    )
    home_team = await db.fetchone(
        "SELECT * FROM teams WHERE guild_id=? AND season=? AND lower(name)=lower(?)",
        (matchup["guild_id"], matchup["season"], matchup["home_team"]),
    )
    embed = discord.Embed(
        title=f"Week {matchup['week']} · {matchup['away_team']} @ {matchup['home_team']}",
        color=0x57F287,
    )
    away_record = f"{away_team['wins']}-{away_team['losses']}-{away_team['ties']}" if away_team else "0-0"
    home_record = f"{home_team['wins']}-{home_team['losses']}-{home_team['ties']}" if home_team else "0-0"
    away_mention = f"<@{matchup['away_user_id']}>" if matchup["away_user_id"] else "Unassigned"
    home_mention = f"<@{matchup['home_user_id']}>" if matchup["home_user_id"] else "Unassigned"
    embed.add_field(
        name=matchup["away_team"],
        value=f"{away_mention} · {away_record}",
    )
    embed.add_field(
        name=matchup["home_team"],
        value=f"{home_mention} · {home_record}",
    )
    if matchup["deadline_at"]:
        from datetime import datetime
        deadline = datetime.fromisoformat(matchup["deadline_at"])
        embed.add_field(
            name="Advance Deadline",
            value=f"<t:{int(deadline.timestamp())}:F> · {settings['timezone']}",
            inline=False,
        )
    streams = []
    for profile in (away_profile, home_profile):
        if profile:
            if profile["twitch"]:
                streams.append(profile["twitch"])
            if profile["youtube"]:
                streams.append(profile["youtube"])
    embed.add_field(name="Streams", value="\n".join(streams) or "No registered links", inline=False)
    embed.set_footer(text="Use the buttons below to coordinate and report the result.")

    return embed


async def post_staff_message(
    interaction: discord.Interaction, db: Database, feature: str,
    destination_field: str, title: str, body: str,
) -> None:
    if await deny_dm(interaction):
        return
    settings = await db.settings(interaction.guild_id)
    if not await require_commissioner(interaction, settings):
        return
    if not settings["features"].get(feature, False):
        await interaction.response.send_message("That module is disabled here.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(
        settings.get(destination_field) or settings.get("announcements_channel_id") or 0
    )
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Run `/setup` to configure channels.", ephemeral=True)
        return
    await channel.send(embed=discord.Embed(title=title, description=body, color=0xFEE75C))
    await db.audit(interaction.guild_id, interaction.user.id, feature, details={"title": title})
    await interaction.response.send_message("Posted.", ephemeral=True)


def run() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LeagueBot(config).run(config.token, log_handler=None)

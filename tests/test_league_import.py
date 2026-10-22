from pathlib import Path
import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leaguebot.db import Database
from leaguebot.helpers import iso_now
from leaguebot.league_import import (
    apply_fixture_import,
    apply_roster_import,
    parse_fixture_import,
    parse_roster_import,
    roster_team_summary,
)
from leaguebot.registration import registration_state
from leaguebot.open_teams_ui import TeamCardView, _team_card_data, _team_card_embed
from leaguebot.ownership import claim_team, initialize_open_teams
from leaguebot.services import WeekRolloverService


DATA = Path(__file__).parents[1] / "output" / "neonsportz-derived"


def test_derived_neonsportz_snapshots_have_expected_shape():
    roster_rows, roster_errors = parse_roster_import(
        (DATA / "madden_team_rosters.csv").read_bytes()
    )
    fixture_rows, fixture_errors = parse_fixture_import(
        (DATA / "madden_18_week_fixtures.csv").read_bytes()
    )

    assert roster_errors == []
    assert fixture_errors == []
    assert len(roster_rows) == 2074
    assert len(roster_team_summary(roster_rows)) == 32
    assert len(fixture_rows) == 272
    assert sorted({row.week for row in fixture_rows}) == list(range(1, 19))
    assert len({row.fixture_id for row in fixture_rows}) == 272
    assert {row.away_team for row in fixture_rows} | {row.home_team for row in fixture_rows} >= {
        "49ers", "Black Knights", "Condors", "Dragons"
    }


def test_raw_games_and_players_exports_are_normalized_without_breaking_canonical_format():
    roster = """rosterId,id,team,fullName,firstName,lastName,position,jerseyNum,playerBestOvr,playerSchemeOvr,devTrait,isActive,isFreeAgent,isRetired,isOnIR,isOnPracticeSquad,injuryType,injuryLength,contractYearsLeft,contractSalary,capHit,yearsPro
555222712,1802395199,Cardinals,Trey McBride,Trey,McBride,TE,85,99,98,3,True,False,False,False,False,98,0,4,53630000,869,4
555000000,1802000000,,Free Agent,Free,Agent,WR,1,70,70,0,True,True,False,False,False,98,0,1,1000000,100,1
555000001,1802000001,,Retired Player,Retired,Player,LT,72,88,88,2,True,False,True,False,False,98,0,0,0,0,12
"""
    fixtures = """id,gameId,league,homeTeam,awayTeam,homeScore,awayScore,seasonIndex,stageIndex,weekIndex,status
27211205,545783824,MADDEN 26 ALL TIME,Raiders,Cardinals,0,0,0,0,0,1
27211206,545783825,MADDEN 26 ALL TIME,Cardinals,Raiders,0,0,0,0,2,1
"""

    roster_rows, roster_errors = parse_roster_import(roster)
    fixture_rows, fixture_errors = parse_fixture_import(fixtures)

    assert roster_errors == []
    assert fixture_errors == []
    assert len(roster_rows) == 1  # Unassigned free agents/retirees are not on a team roster.
    player = roster_rows[0]
    assert (player.player_id, player.player_name, player.team_abbr) == (
        "555222712", "Trey McBride", "ARI"
    )
    assert player.values["overall"] == "99"
    assert player.values["jersey_number"] == "85"
    assert [row.week for row in fixture_rows] == [1, 3]
    assert fixture_rows[0].fixture_id == "545783824"
    assert fixture_rows[0].away_team_id == player.team_id
    assert fixture_rows[0].away_abbr == "ARI"


def test_raw_player_without_team_or_unassigned_flag_is_still_invalid():
    rows, errors = parse_roster_import(
        "rosterId,team,fullName,position,isFreeAgent,isRetired\n"
        "1,,Missing Team,QB,False,False\n"
    )
    assert rows == []
    assert any("team, player ID" in error for error in errors)


def _raw_roster_export():
    canonical, errors = parse_roster_import((DATA / "madden_team_rosters.csv").read_bytes())
    assert not errors
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rosterId", "team", "fullName", "position", "playerBestOvr",
                     "isFreeAgent", "isRetired"])
    for player in canonical:
        writer.writerow([player.player_id, player.team_name, player.player_name,
                         player.position, player.values["overall"], False, False])
    writer.writerow(["retired", "", "Retired Player", "QB", 80, False, True])
    return output.getvalue().encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_format", [False, True])
async def test_same_importrosters_command_previews_both_formats(tmp_path, monkeypatch, raw_format):
    from leaguebot import bot as bot_module
    from leaguebot.bot import LeagueBot, register_commands
    from leaguebot.config import Config

    bot = LeagueBot(Config.from_env(require_token=False))
    bot.db = Database(tmp_path / "command.sqlite3")
    await bot.db.initialize()
    register_commands(bot)
    monkeypatch.setattr(bot_module, "require_commissioner", AsyncMock(return_value=True))
    content = _raw_roster_export() if raw_format else (DATA / "madden_team_rosters.csv").read_bytes()
    attachment = SimpleNamespace(
        filename="MAD27_players.csv" if raw_format else "madden_team_rosters.csv",
        size=len(content), read=AsyncMock(return_value=content),
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123), guild_id=123, user=SimpleNamespace(id=99),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    try:
        await bot.tree.get_command("importrosters").callback(interaction, attachment)
        preview = interaction.followup.send.call_args.kwargs
        assert "32 teams" in preview["embed"].description
        assert len(preview["view"].rows) == 2074
        assert not await bot.db.fetchall("SELECT * FROM roster_players")
    finally:
        await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_first", [False, True])
async def test_switching_roster_formats_preserves_franchise_ids_and_ownership(tmp_path, raw_first):
    db = Database(tmp_path / "formats.sqlite3")
    await db.initialize()
    canonical, _ = parse_roster_import((DATA / "madden_team_rosters.csv").read_bytes())
    raw, errors = parse_roster_import(_raw_roster_export())
    assert not errors
    await apply_roster_import(db, 123, "1", raw if raw_first else canonical)
    team = await db.fetchone("SELECT * FROM franchises WHERE team_name='49ers'")
    await db.execute(
        """INSERT INTO profiles (guild_id,user_id,team_name,external_team_id,approved,updated_at)
           VALUES (123,99,'49ers',?,1,'now')""", (team["external_team_id"],),
    )
    for rows in (raw, raw, canonical):
        assert await apply_roster_import(db, 123, "1", rows) == (32, 2074)
        updated = await db.fetchone("SELECT * FROM franchises WHERE team_name='49ers'")
        assert updated["external_team_id"] == team["external_team_id"]
        owner = await db.fetchone("SELECT * FROM profiles WHERE guild_id=123 AND user_id=99")
        assert owner["external_team_id"] == team["external_team_id"]
        assert owner["approved"] == 1
        assert (await db.fetchone(
            "SELECT COUNT(*) AS n FROM roster_players WHERE guild_id=123 AND season='1'"
        ))["n"] == 2074


@pytest.mark.asyncio
async def test_full_import_starts_week_one_and_keeps_all_future_fixtures(tmp_path):
    db = Database(tmp_path / "league.sqlite3")
    await db.initialize()
    await db.settings(123)
    roster_rows, _ = parse_roster_import((DATA / "madden_team_rosters.csv").read_bytes())
    fixture_rows, _ = parse_fixture_import((DATA / "madden_18_week_fixtures.csv").read_bytes())

    teams, players = await apply_roster_import(db, 123, "1", roster_rows)
    created, updated, weeks = await apply_fixture_import(
        db, 123, "1", fixture_rows, start_now=True
    )

    assert (teams, players) == (32, 2074)
    assert (created, updated, weeks) == (272, 0, 18)
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM matchups"))["n"] == 272
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM roster_players"))["n"] == 2074
    settings = await db.settings(123)
    assert settings["current_week"] == 1
    assert settings["auto_week_rollover"] == 1
    assert settings["regular_season_weeks"] == 18
    assert settings["week_deadline_at"]

    state = await registration_state(db, 123, "1")
    assert len(state.all_teams) == 32
    assert "Dragons" in state.all_teams
    assert "Cowboys" not in state.all_teams


class _FakeAI:
    available = False


class _FakeGuild:
    id = 123

    def get_channel(self, _channel_id):
        return None


class _FakeBot:
    ai = _FakeAI()

    def __init__(self):
        self.guild = _FakeGuild()

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


@pytest.mark.asyncio
async def test_rollover_opens_next_week_and_keeps_unresolved_game(tmp_path, monkeypatch):
    db = Database(tmp_path / "rollover.sqlite3")
    await db.initialize()
    await db.settings(123)
    roster_rows, _ = parse_roster_import((DATA / "madden_team_rosters.csv").read_bytes())
    fixture_rows, _ = parse_fixture_import((DATA / "madden_18_week_fixtures.csv").read_bytes())
    await apply_roster_import(db, 123, "1", roster_rows)
    await apply_fixture_import(db, 123, "1", fixture_rows, start_now=True)

    created_weeks = []
    deleted_weeks = []

    async def fake_create(_interaction, _db, *, season, week):
        created_weeks.append((season, week))
        return 16, []

    async def fake_delete(_guild, _db, season, week):
        deleted_weeks.append((season, week))
        return []

    monkeypatch.setattr("leaguebot.channel_workflow.create_week_matchup_channels", fake_create)
    monkeypatch.setattr("leaguebot.channel_workflow.lock_and_delete_week_channels", fake_delete)

    service = WeekRolloverService(_FakeBot(), db)
    ok, _ = await service.rollover(123, expected_week=1, actor_id=99)

    assert ok is True
    assert (await db.settings(123))["current_week"] == 2
    assert created_weeks == [("1", 2)]
    assert deleted_weeks == [("1", 1)]
    unresolved = await db.fetchone(
        "SELECT COUNT(*) AS n FROM matchups WHERE guild_id=123 AND season='1' AND week=1 AND status='waiting'"
    )
    assert unresolved["n"] > 0
    audit = await db.fetchone("SELECT * FROM week_rollovers WHERE guild_id=123 AND from_week=1")
    assert audit["status"] == "complete"
    assert audit["unresolved_count"] == unresolved["n"]
@pytest.mark.asyncio
async def test_individual_team_cards_show_roster_and_registration_state(tmp_path):
    db = Database(tmp_path / "cards.sqlite3")
    await db.initialize()
    await db.settings(456)
    roster_rows, _ = parse_roster_import((DATA / "madden_team_rosters.csv").read_bytes())
    await apply_roster_import(db, 456, "1", roster_rows)
    await initialize_open_teams(db, 456, "1")

    cards = await _team_card_data(db, 456, "1")
    assert len(cards) == 32
    card = next(item for item in cards if item["team_name"] == "49ers")
    assert card["is_open"] is True
    assert len(card["roster"]) > 50
    assert "Roster" in _team_card_embed(card, "1").fields[1].name

    view = TeamCardView(456, card["external_team_id"], is_open=True)
    assert len(view.children) == 2
    assert view.children[0].item.label == "View Team"
    assert view.children[0].item.custom_id.startswith("leaguebot:team-card:roster:456:")
    assert view.children[1].item.label == "Claim Team"
    assert view.children[1].item.custom_id.startswith("leaguebot:team-card:claim:456:")

    await claim_team(db, 456, "1", 999, "49ers")
    updated = next(item for item in await _team_card_data(db, 456, "1") if item["team_name"] == "49ers")
    assert updated["is_open"] is False
    assert updated["status_text"] == "Owner: <@999>"
    assert [item.item.label for item in TeamCardView(
        456, updated["external_team_id"], is_open=False
    ).children] == ["View Team"]

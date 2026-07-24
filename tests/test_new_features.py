import pytest

from leaguebot.ai import deterministic_rankings, sanitize_ai_text
from leaguebot.db import Database
from leaguebot.helpers import iso_now
from leaguebot.member_import import MemberImportRow, apply_member_import


def test_ai_output_sanitizes_mass_mentions_and_discord_fences():
    value = sanitize_ai_text("@everyone <@&123> ```danger```")
    assert "@everyone" not in value
    assert "<@&123>" not in value
    assert "```" not in value


@pytest.mark.asyncio
async def test_merge_import_preserves_omitted_matchup_owner(tmp_path):
    db = Database(tmp_path / "members.sqlite3")
    await db.initialize()
    now = iso_now()
    await db.execute(
        """INSERT INTO profiles
           (guild_id,user_id,team_name,approved,assignment_source,assigned_at,updated_at)
           VALUES (1,10,'49ers',1,'commissioner',?,?),(1,20,'Cowboys',1,'commissioner',?,?)""",
        (now, now, now, now),
    )
    await db.execute(
        """INSERT INTO matchups
           (guild_id,season,week,external_key,away_team,home_team,away_user_id,home_user_id,
            status,created_at,updated_at)
           VALUES (1,'1',1,'one','49ers','Cowboys',10,20,'waiting',?,?)""",
        (now, now),
    )
    await apply_member_import(
        db, 1, "1", (MemberImportRow(2, 10, "one", "49ers"),), "merge"
    )
    matchup = await db.fetchone("SELECT * FROM matchups WHERE external_key='one'")
    assert matchup["away_user_id"] == 10
    assert matchup["home_user_id"] == 20


@pytest.mark.asyncio
async def test_deterministic_rankings_use_stable_tiebreaks(tmp_path):
    db = Database(tmp_path / "rank.sqlite3")
    await db.initialize()
    await db.execute(
        """INSERT INTO teams (guild_id,season,name,wins,losses,ties)
           VALUES (1,'1','Cowboys',1,0,0),(1,'1','49ers',1,0,0)"""
    )
    rankings = await deterministic_rankings(db, 1, "1", 1)
    assert [row["team"] for row in rankings] == ["49ers", "Cowboys"]

import json

import pytest

from leaguebot.db import Database


@pytest.mark.asyncio
async def test_guild_settings_are_isolated(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.initialize()
    await db.update_settings(100, league_name="Madden")
    await db.update_settings(200, league_name="College")
    assert (await db.settings(100))["league_name"] == "Madden"
    assert (await db.settings(200))["league_name"] == "College"


@pytest.mark.asyncio
async def test_audit_log_is_scoped(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.initialize()
    await db.audit(100, 9, "setup", details={"ok": True})
    row = await db.fetchone("SELECT * FROM audit_logs WHERE guild_id=?", (100,))
    assert json.loads(row["details"]) == {"ok": True}


@pytest.mark.asyncio
async def test_unknown_setting_is_rejected(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.initialize()
    with pytest.raises(ValueError):
        await db.update_settings(100, secret="bad")


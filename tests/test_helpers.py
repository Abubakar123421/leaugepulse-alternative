from datetime import UTC, datetime

from leaguebot.helpers import next_deadline, parse_user_datetime, stable_game_key


def test_deadline_rolls_to_next_week_if_time_passed():
    now = datetime(2026, 7, 20, 22, tzinfo=UTC)  # Monday
    result = next_deadline("UTC", 0, "21:00", now=now)
    assert result == datetime(2026, 7, 27, 21, tzinfo=UTC)


def test_user_datetime_converts_timezone():
    result = parse_user_datetime("2026-07-20 21:00", "America/New_York")
    assert result.hour == 1
    assert result.day == 21


def test_game_key_changes_by_week():
    assert stable_game_key(1, "A", "B") != stable_game_key(2, "A", "B")


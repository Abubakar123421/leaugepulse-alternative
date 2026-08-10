from leaguebot.member_import import MEMBER_CSV_FORMAT, parse_member_csv


class _Member:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.global_name = None

    def __str__(self) -> str:
        return self.name


class _Guild:
    def __init__(self):
        self.members = [_Member(123456789012345678, "coach")]

    def get_member(self, user_id: int):
        return next((member for member in self.members if member.id == user_id), None)


def test_member_csv_missing_headers_shows_required_format():
    preview = parse_member_csv(b"name,club\ncoach,49ers\n", _Guild(), ("49ers",))

    assert preview.rows == ()
    assert any("Missing a team header" in error for error in preview.errors)
    assert any("Missing a member header" in error for error in preview.errors)
    assert MEMBER_CSV_FORMAT in preview.errors


def test_member_csv_accepts_recommended_headers_and_discord_id():
    preview = parse_member_csv(
        b"team,discord_id,twitch,youtube\n49ers,123456789012345678,,\n",
        _Guild(),
        ("49ers",),
    )

    assert preview.errors == ()
    assert len(preview.rows) == 1
    assert preview.rows[0].user_id == 123456789012345678
    assert preview.rows[0].team_name == "49ers"

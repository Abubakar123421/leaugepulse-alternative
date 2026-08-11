from types import SimpleNamespace

from leaguebot.welcome import member_welcome_embed


def test_member_welcome_embed_uses_member_and_guild_settings():
    member = SimpleNamespace(
        mention="<@123456789>",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
    )

    embed = member_welcome_embed(
        member,
        game="Madden 27",
        league_name="Gridiron Dynasty",
    )

    assert embed.title == "🔥🏈 WELCOME TO THE MADDEN 27 LEAGUE! 🏈🔥"
    assert "Welcome <@123456789>" in embed.description
    assert "BUILD YOUR DYNASTY & WIN THE SUPER BOWL" in embed.description
    assert any("48 HOURS" in field.value for field in embed.fields)
    assert any("Claim & represent your team" in field.value for field in embed.fields)
    assert embed.thumbnail.url == "https://cdn.example/avatar.png"
    assert embed.footer.text == "Gridiron Dynasty • Welcome to the league"

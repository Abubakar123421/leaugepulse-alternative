from __future__ import annotations

import discord


def member_welcome_embed(
    member: discord.Member,
    *,
    game: str,
    league_name: str,
) -> discord.Embed:
    """Build the public welcome card shown when a member joins a league guild."""
    game_name = (game or "Madden 27").strip().upper()
    embed = discord.Embed(
        title=f"🔥🏈 WELCOME TO THE {game_name} LEAGUE! 🏈🔥",
        description=(
            f"Welcome {member.mention} — **you’re officially in!** 🎮\n\n"
            "You just joined a **32-team competitive franchise** where every game, "
            "trade, draft pick, rivalry, and roster decision matters.\n\n"
            "🏆 **THE MISSION IS SIMPLE:**\n"
            "## BUILD YOUR DYNASTY & WIN THE SUPER BOWL."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="🏟️ BEFORE YOU GET STARTED",
        value=(
            "📜 Read the league rules\n"
            "🏈 Claim & represent your team\n"
            "👨‍💼 Create your custom coach\n"
            "🎥 Post your Twitch/streaming information\n"
            "📅 Schedule your user games\n"
            "💬 Talk your trash & get involved\n"
            "🔥 Most importantly — **COMPETE & HAVE FUN!**"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ STAY READY",
        value=(
            "We advance every **48 HOURS**, so stay active, communicate with your "
            "opponents, and respect everyone’s time."
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 YOUR FRANCHISE STARTS NOW",
        value=(
            "There are **32 teams**, but only **ONE Super Bowl Champion.**\n\n"
            "Good luck this season… **you’re gonna need it.** 😈🔥🏈"
        ),
        inline=False,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{league_name} • Welcome to the league")
    return embed

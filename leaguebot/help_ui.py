from __future__ import annotations

import discord


CATEGORIES = {
    "Getting Started": {"setup", "help", "settings", "destinations"},
    "Player Registration": {"register", "registerteam", "profile", "roster", "playersearch"},
    "Matchups and Scheduling": {
        "week", "createweek", "profile", "roster", "playersearch", "season-history", "leaderboard"
    },
    "Career and History": {"profile", "leaderboard", "season-history"},
    "Commissioner Commands": {
        "setup", "settings", "profile-approve", "assign-team", "team-release",
        "importrosters", "importfixtures", "importmembers", "import-schedule", "week", "season-close", "season-force-delete",
        "season-cleanup", "announce", "award", "seasonaward", "seasonawards", "gameoftheweek", "weekmvp", "syncteamemojis", "setteamemoji", "backup", "createweek",
        "destinations", "setannouncementchannel",
        "setscorechannel", "setstreamchannel", "setstorylinechannel",
        "settradechannel", "settransactionchannel", "setauditchannel",
        "setopenteamlist", "setopenchannel", "setpollchannel", "setrecruitingchannel",
    },
    "Trades and Transfers": {"trade-block", "transaction", "open-roster"},
    "Streams and Profiles": {"register", "profile"},
}


class HelpView(discord.ui.View):
    def __init__(self, commands: list[discord.app_commands.Command], commissioner: bool):
        super().__init__(timeout=600)
        self.commands = commands
        self.commissioner = commissioner
        self.categories = list(CATEGORIES)
        if not commissioner:
            self.categories.remove("Commissioner Commands")
        self.index = 0
        self.category.options = [
            discord.SelectOption(label=name, value=name) for name in self.categories
        ]

    def embed(self) -> discord.Embed:
        category = self.categories[self.index]
        allowed = CATEGORIES[category]
        lines = []
        for command in sorted(self.commands, key=lambda item: item.name):
            if command.name in allowed:
                lines.append(f"**/{command.name}** — {command.description}")
        embed = discord.Embed(
            title=f"League Bot Help · {category}",
            description="\n".join(lines) or "No commands are available in this category.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{self.index + 1} of {len(self.categories)}")
        return embed

    @discord.ui.select(placeholder="Choose a help category")
    async def category(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.index = self.categories.index(select.values[0])
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Home", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = 0
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index - 1) % len(self.categories)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index + 1) % len(self.categories)
        await interaction.response.edit_message(embed=self.embed(), view=self)

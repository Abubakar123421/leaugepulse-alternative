from __future__ import annotations

from dataclasses import dataclass

import discord

from .db import Database
from .team_roles import active_team_names


@dataclass(frozen=True, slots=True)
class TeamRegistrationState:
    canonical_names: dict[str, str]
    taken: dict[str, int]
    pending: dict[str, int]

    @property
    def all_teams(self) -> list[str]:
        return sorted(self.canonical_names.values(), key=str.casefold)

    def canonical(self, requested: str) -> str | None:
        return self.canonical_names.get(normalize_team_name(requested))

    def available_for(self, user_id: int) -> list[str]:
        return [
            team
            for team in self.all_teams
            if self.taken.get(normalize_team_name(team), user_id) == user_id
            and self.pending.get(normalize_team_name(team), user_id) == user_id
        ]


async def registration_state(
    db: Database, guild_id: int, season: str
) -> TeamRegistrationState:
    canonical_names = {
        normalize_team_name(team): team
        for team in await active_team_names(db, guild_id, season)
    }

    profile_rows = await db.fetchall(
        """SELECT user_id, team_name, approved FROM profiles
           WHERE guild_id=?""",
        (guild_id,),
    )
    taken: dict[str, int] = {}
    pending: dict[str, int] = {}
    for row in profile_rows:
        normalized = normalize_team_name(row["team_name"])
        if normalized not in canonical_names:
            continue
        destination = taken if row["approved"] else pending
        destination[normalized] = row["user_id"]

    return TeamRegistrationState(canonical_names, taken, pending)


def registration_rejection_embed(
    state: TeamRegistrationState,
    *,
    reason: str,
    user_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Team Registration Not Accepted",
        description=reason,
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Available Teams",
        value=_team_list(state.available_for(user_id), "None currently available"),
        inline=False,
    )
    embed.add_field(
        name="Taken Teams",
        value=_team_list(
            [
                state.canonical_names[name]
                for name, owner_id in state.taken.items()
                if owner_id != user_id
            ],
            "None",
        ),
        inline=False,
    )
    embed.add_field(
        name="Pending Review",
        value=_team_list(
            [
                state.canonical_names[name]
                for name, owner_id in state.pending.items()
                if owner_id != user_id
            ],
            "None",
        ),
        inline=False,
    )
    embed.set_footer(
        text="Team names ignore letter case, but spelling must match one of the imported league teams."
    )
    return embed


def normalize_team_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _team_list(teams: list[str], empty: str) -> str:
    value = ", ".join(sorted(set(teams), key=str.casefold)) or empty
    return value[:1024]

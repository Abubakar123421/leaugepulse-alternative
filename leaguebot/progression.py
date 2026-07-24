from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt

from .helpers import iso_now


WIN_XP = 100
LOSS_XP = 50
FORCE_WIN_XP = 60
FAIR_SIM_XP = 15
CHAMPIONSHIP_XP = 500


@dataclass(frozen=True, slots=True)
class ProgressAward:
    user_id: int
    team_name: str
    games: int = 0
    wins: int = 0
    losses: int = 0
    force_wins: int = 0
    forfeits: int = 0
    sims: int = 0
    points_for: int = 0
    points_against: int = 0
    xp: int = 0


def level_for_xp(xp: int) -> int:
    """Non-linear levels: 200 XP for L2, 800 for L3, 1,800 for L4."""
    return 1 + floor(sqrt(max(0, xp) / 200))


def next_level_xp(level: int) -> int:
    return max(1, level) ** 2 * 200


def win_ratio(wins: int, losses: int) -> float:
    decisions = wins + losses
    return (wins / decisions * 100) if decisions else 0.0


def matchup_awards(matchup, outcome: str) -> list[ProgressAward]:
    away_id = matchup["away_user_id"]
    home_id = matchup["home_user_id"]
    if outcome == "complete":
        if matchup["away_score"] is None or matchup["home_score"] is None:
            return []
        away_won = matchup["away_score"] > matchup["home_score"]
        return [
            ProgressAward(
                away_id,
                matchup["away_team"],
                games=1,
                wins=int(away_won),
                losses=int(not away_won),
                points_for=matchup["away_score"],
                points_against=matchup["home_score"],
                xp=WIN_XP if away_won else LOSS_XP,
            )
            for away_id in [away_id]
            if away_id
        ] + [
            ProgressAward(
                home_id,
                matchup["home_team"],
                games=1,
                wins=int(not away_won),
                losses=int(away_won),
                points_for=matchup["home_score"],
                points_against=matchup["away_score"],
                xp=WIN_XP if not away_won else LOSS_XP,
            )
            for home_id in [home_id]
            if home_id
        ]
    if outcome in ("force_away", "force_home", "concession_away", "concession_home"):
        away_won = outcome.endswith("away")
        return [
            ProgressAward(
                away_id,
                matchup["away_team"],
                wins=int(away_won),
                losses=int(not away_won),
                force_wins=int(away_won),
                forfeits=int(not away_won),
                xp=FORCE_WIN_XP if away_won else 0,
            )
            for away_id in [away_id]
            if away_id
        ] + [
            ProgressAward(
                home_id,
                matchup["home_team"],
                wins=int(not away_won),
                losses=int(away_won),
                force_wins=int(not away_won),
                forfeits=int(away_won),
                xp=FORCE_WIN_XP if not away_won else 0,
            )
            for home_id in [home_id]
            if home_id
        ]
    if outcome == "fair_sim":
        return [
            ProgressAward(
                user_id,
                team_name,
                sims=1,
                xp=FAIR_SIM_XP,
            )
            for user_id, team_name in (
                (away_id, matchup["away_team"]),
                (home_id, matchup["home_team"]),
            )
            if user_id
        ]
    return []


async def record_matchup_progress(conn, matchup, outcome: str) -> None:
    for award in matchup_awards(matchup, outcome):
        source_key = f"{matchup['week']}:{matchup['external_key']}"
        cursor = await conn.execute(
            """INSERT INTO career_events
               (guild_id,season,source_key,user_id,event_type,xp,created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(guild_id,season,source_key,user_id,event_type)
               DO NOTHING""",
            (
                matchup["guild_id"],
                matchup["season"],
                source_key,
                award.user_id,
                outcome,
                award.xp,
                iso_now(),
            ),
        )
        if cursor.rowcount != 1:
            continue
        await conn.execute(
            """INSERT INTO career_profiles (guild_id,user_id,created_at,updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(guild_id,user_id) DO NOTHING""",
            (matchup["guild_id"], award.user_id, iso_now(), iso_now()),
        )
        await conn.execute(
            """INSERT INTO season_participants
               (guild_id,season,user_id,team_name,joined_at,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(guild_id,season,user_id,team_name) DO NOTHING""",
            (
                matchup["guild_id"],
                matchup["season"],
                award.user_id,
                award.team_name,
                iso_now(),
                iso_now(),
            ),
        )
        values = (
            award.games,
            award.wins,
            award.losses,
            award.force_wins,
            award.forfeits,
            award.sims,
            award.points_for,
            award.points_against,
            award.xp,
            iso_now(),
        )
        await conn.execute(
            """UPDATE career_profiles SET
               games=games+?,wins=wins+?,losses=losses+?,
               force_wins=force_wins+?,forfeits=forfeits+?,sims=sims+?,
               points_for=points_for+?,points_against=points_against+?,
               xp=xp+?,updated_at=?
               WHERE guild_id=? AND user_id=?""",
            (*values, matchup["guild_id"], award.user_id),
        )
        await conn.execute(
            """UPDATE season_participants SET
               games=games+?,wins=wins+?,losses=losses+?,
               force_wins=force_wins+?,forfeits=forfeits+?,sims=sims+?,
               points_for=points_for+?,points_against=points_against+?,
               xp=xp+?,updated_at=?
               WHERE guild_id=? AND season=? AND user_id=? AND team_name=?""",
            (
                *values,
                matchup["guild_id"],
                matchup["season"],
                award.user_id,
                award.team_name,
            ),
        )


async def reverse_matchup_progress(conn, matchup, outcome: str) -> None:
    """Reverse a previously recorded active-season outcome exactly once."""
    for award in matchup_awards(matchup, outcome):
        source_key = f"{matchup['week']}:{matchup['external_key']}"
        cursor = await conn.execute(
            """DELETE FROM career_events
               WHERE guild_id=? AND season=? AND source_key=?
               AND user_id=? AND event_type=?""",
            (
                matchup["guild_id"],
                matchup["season"],
                source_key,
                award.user_id,
                outcome,
            ),
        )
        if cursor.rowcount != 1:
            continue
        values = (
            award.games,
            award.wins,
            award.losses,
            award.force_wins,
            award.forfeits,
            award.sims,
            award.points_for,
            award.points_against,
            award.xp,
            iso_now(),
        )
        await conn.execute(
            """UPDATE career_profiles SET
               games=MAX(0,games-?),wins=MAX(0,wins-?),losses=MAX(0,losses-?),
               force_wins=MAX(0,force_wins-?),forfeits=MAX(0,forfeits-?),
               sims=MAX(0,sims-?),points_for=MAX(0,points_for-?),
               points_against=MAX(0,points_against-?),xp=MAX(0,xp-?),updated_at=?
               WHERE guild_id=? AND user_id=?""",
            (*values, matchup["guild_id"], award.user_id),
        )
        await conn.execute(
            """UPDATE season_participants SET
               games=MAX(0,games-?),wins=MAX(0,wins-?),losses=MAX(0,losses-?),
               force_wins=MAX(0,force_wins-?),forfeits=MAX(0,forfeits-?),
               sims=MAX(0,sims-?),points_for=MAX(0,points_for-?),
               points_against=MAX(0,points_against-?),xp=MAX(0,xp-?),updated_at=?
               WHERE guild_id=? AND season=? AND user_id=? AND team_name=?""",
            (
                *values,
                matchup["guild_id"],
                matchup["season"],
                award.user_id,
                award.team_name,
            ),
        )

async def ensure_participant(
    conn,
    *,
    guild_id: int,
    season: str,
    user_id: int,
    team_name: str,
) -> None:
    now = iso_now()
    await conn.execute(
        """INSERT INTO career_profiles (guild_id,user_id,created_at,updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(guild_id,user_id) DO NOTHING""",
        (guild_id, user_id, now, now),
    )
    await conn.execute(
        """INSERT INTO season_participants
           (guild_id,season,user_id,team_name,joined_at,updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(guild_id,season,user_id,team_name) DO NOTHING""",
        (guild_id, season, user_id, team_name, now, now),
    )


async def award_championship(
    conn,
    *,
    guild_id: int,
    season: str,
    user_id: int,
    team_name: str,
) -> bool:
    cursor = await conn.execute(
        """INSERT INTO career_events
           (guild_id,season,source_key,user_id,event_type,xp,created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(guild_id,season,source_key,user_id,event_type) DO NOTHING""",
        (
            guild_id,
            season,
            "season-championship",
            user_id,
            "championship",
            CHAMPIONSHIP_XP,
            iso_now(),
        ),
    )
    if cursor.rowcount != 1:
        return False
    await ensure_participant(
        conn,
        guild_id=guild_id,
        season=season,
        user_id=user_id,
        team_name=team_name,
    )
    await conn.execute(
        """UPDATE career_profiles
           SET championships=championships+1,xp=xp+?,updated_at=?
           WHERE guild_id=? AND user_id=?""",
        (CHAMPIONSHIP_XP, iso_now(), guild_id, user_id),
    )
    await conn.execute(
        """UPDATE season_participants
           SET champion=1,xp=xp+?,updated_at=?
           WHERE guild_id=? AND season=? AND user_id=? AND team_name=?""",
        (CHAMPIONSHIP_XP, iso_now(), guild_id, season, user_id, team_name),
    )
    return True

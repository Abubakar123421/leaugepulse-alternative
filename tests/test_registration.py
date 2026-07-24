from leaguebot.registration import TeamRegistrationState, normalize_team_name


def test_team_name_matching_ignores_case_and_extra_spaces():
    state = TeamRegistrationState(
        canonical_names={"san francisco 49ers": "San Francisco 49ers"},
        taken={},
        pending={},
    )
    assert state.canonical("  SAN   FRANCISCO 49ERS ") == "San Francisco 49ers"


def test_available_teams_exclude_other_members_claims():
    state = TeamRegistrationState(
        canonical_names={"49ers": "49ers", "cowboys": "Cowboys", "giants": "Giants"},
        taken={"49ers": 100},
        pending={"cowboys": 200},
    )
    assert state.available_for(300) == ["Giants"]
    assert state.available_for(100) == ["49ers", "Giants"]


def test_normalization_does_not_allow_different_spelling():
    assert normalize_team_name("49ERS") == normalize_team_name("49ers")
    assert normalize_team_name("49er") != normalize_team_name("49ers")

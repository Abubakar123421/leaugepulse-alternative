from leaguebot.imports import parse_schedule_csv


def test_parses_aliases_and_stable_keys():
    text = "Week Number,Visitor Team,Home Team,Game ID\n1,Jets,Bears,abc\n"
    first, errors = parse_schedule_csv(text)
    second, _ = parse_schedule_csv(text)
    assert errors == []
    assert first[0].away_team == "Jets"
    assert first[0].external_key == second[0].external_key


def test_reports_missing_columns():
    games, errors = parse_schedule_csv("week,team\n1,Jets\n")
    assert games == []
    assert len(errors) == 2


def test_rejects_bad_rows_without_losing_good_rows():
    text = "week,away,home\n1,A,B\n0,A,A\n"
    games, errors = parse_schedule_csv(text)
    assert len(games) == 1
    assert errors and errors[0].startswith("Row 3:")


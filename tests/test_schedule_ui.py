from leaguebot.schedule_ui import ScheduleDecisionView


def test_schedule_decision_ids_include_proposal_version():
    view = ScheduleDecisionView(12, 4)
    assert {item.custom_id for item in view.children} == {
        "leaguebot:schedule:accept:12:4",
        "leaguebot:schedule:counter:12:4",
        "leaguebot:schedule:decline:12:4",
    }

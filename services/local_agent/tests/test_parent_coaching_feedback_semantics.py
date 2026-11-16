from copy import deepcopy
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report


CHILD = "child-primary-4"
OTHER = "child-junior-3"
CONTEXT = {
    "focus": "reported_difficulty", "child_stage": "四年级",
    "subjects": ["数学"], "version": "local_rules_v2",
}


def material(feedback=()):
    return {
        "session": {}, "child": {"child_id": CHILD, "grade": "四年级"},
        "reflection": {"difficult_text": "应用题", "learned_text": "", "next_action": "", "feeling": ""},
        "tasks": [], "recalls": [{"subject": "数学", "summary": "应用题"}],
        "coaching_feedback": list(feedback),
    }


def action(**changes):
    return {
        "feedback_kind": "action_execution", "child_id": CHILD,
        "binding_status": "bound",
        "executed": 1, "effectiveness": 2, "stress_rating": 1,
        "feedback_context": deepcopy(CONTEXT), **changes,
    }


@pytest.mark.parametrize("executed", [False, 0, None, "1"])
def test_unexecuted_feedback_does_not_change_effectiveness_or_duration(executed):
    _, baseline = build_daily_report(material())
    _, coaching = build_daily_report(material([action(executed=executed, stress_rating=5)]))
    assert coaching["adaptation"]["average_effectiveness"] is None
    assert coaching["adaptation"]["average_stress"] is None
    assert coaching["adaptation"]["feedback_count"] == 0
    assert coaching["action"] == baseline["action"]


@pytest.mark.parametrize("context_change", [
    {"focus": "unfinished_task"}, {"child_stage": "初三"},
    {"subjects": ["英语"]}, {"version": "different_strategy_v2"},
])
def test_other_context_does_not_average_with_current_action(context_change):
    other = action(effectiveness=5, stress_rating=5,
                   feedback_context={**CONTEXT, **context_change})
    _, coaching = build_daily_report(material([other, action()]))
    assert coaching["adaptation"]["average_effectiveness"] == 2
    assert coaching["adaptation"]["average_stress"] == 1
    assert coaching["adaptation"]["feedback_count"] == 1
    assert coaching["action"]["duration_minutes"] == 7


def test_unknown_context_and_report_usefulness_are_not_execution_evidence():
    _, coaching = build_daily_report(material([
        action(feedback_context={}),
        action(feedback_kind="report_helpfulness", helpfulness=5),
        action(child_id=OTHER),
    ]))
    assert coaching["adaptation"]["basis"] == "cold_start"
    assert coaching["adaptation"]["average_effectiveness"] is None


def test_recent_other_scenarios_do_not_hide_matching_action_feedback():
    unrelated = action(feedback_context={**CONTEXT, "focus": "empty_day"}, effectiveness=5)
    _, coaching = build_daily_report(material([unrelated] * 6 + [action()]))
    assert coaching["adaptation"]["average_effectiveness"] == 2
    assert coaching["adaptation"]["feedback_count"] == 1


@pytest.fixture
def repository(tmp_path):
    db = Database(tmp_path / "coaching-semantics.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    return repo


def session(repo, child=CHILD, difficult="应用题", subject="数学"):
    sid = repo.start_session(child, str(uuid4()))["session_id"]
    repo.add_recall(sid, subject, "今天学习了" + difficult, str(uuid4()))
    with repo.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    repo.complete_with_reflection(sid, "", difficult, "还可以", "", False, str(uuid4()))
    return sid


def report(repo, *, source="local_rules_v2", version=None, **kwargs):
    sid = session(repo, **kwargs)
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    if version:
        coaching["feedback_context"]["version"] = version
    return repo.save_daily_report(sid, summary, coaching, source=source)


def bound_feedback(repo, saved, executed=True, effectiveness=2, stress_rating=1, revision=0):
    action = saved["parent_action"]
    return repo.save_parent_report_feedback(saved["report_id"], executed, effectiveness, stress_rating, "明确行动反馈",
        action_snapshot_id=action["snapshot_id"], client_action_id=str(uuid4()), expected_revision=revision,
        executed_at=action["created_at"] if executed else None)


def test_snapshot_helpfulness_is_preserved_without_inventing_execution(repository):
    repo = repository
    saved = report(repo)
    snapshots = ParentReportService(repo.database)
    snapshot = snapshots.create_from_daily(saved)
    snapshot = snapshots.approve(snapshot["snapshot_id"])
    snapshots.record_feedback(snapshot["snapshot_id"], helpfulness=5, stress_rating=5,
                              notes="报告有用，行动还没有做", client_action_id="helpful-not-executed")
    next_session = session(repo)
    restarted = StudyRepository(Database(repo.database.path))
    evidence = restarted.report_evidence(next_session)
    assert evidence["coaching_feedback"] == []
    rating = evidence["report_feedback"][0]
    assert rating["helpfulness"] == 5
    assert rating["snapshot_id"] == snapshot["snapshot_id"]
    assert rating["source_version"] == snapshot["source_version"]
    assert "executed" not in rating and "effectiveness" not in rating
    _, coaching = build_daily_report(evidence)
    assert coaching["adaptation"]["average_effectiveness"] is None
    assert coaching["adaptation"]["average_stress"] is None
    assert coaching["action"]["duration_minutes"] == 10
    assert coaching["report_feedback"]["latest_helpfulness"] == 5
    bound_feedback(repo, repo.get_daily_report(saved['report_id']))
    _, executed = build_daily_report(restarted.report_evidence(next_session))
    assert executed["adaptation"]["average_effectiveness"] == 2
    assert executed["adaptation"]["feedback_count"] == 1
    assert executed["report_feedback"]["feedback_count"] == 1
    assert restarted.report_evidence(session(repo, child=OTHER))["report_feedback"] == []


def test_draft_rating_is_retained_but_not_effective_before_real_parent_approval(repository):
    repo = repository
    saved = report(repo)
    snapshots = ParentReportService(repo.database)
    draft = snapshots.create_from_daily(saved)
    snapshots.record_feedback(draft['snapshot_id'], helpfulness=5, stress_rating=5,
        notes='草稿评价不代表行动执行', client_action_id='draft-rating')
    persisted = snapshots.get(draft['snapshot_id'])
    assert persisted['status']=='draft'
    assert persisted['feedback'][0]['helpfulness']==5
    assert persisted['feedback_current'] is None
    next_session = session(repo)
    evidence = repo.report_evidence(next_session)
    assert evidence['report_feedback']==[]
    assert evidence['coaching_feedback']==[]
    assert build_daily_report(evidence)[1]['adaptation']['average_stress'] is None
    snapshots.approve(draft['snapshot_id'])
    restarted = StudyRepository(Database(repo.database.path))
    current = restarted.report_evidence(next_session)
    assert current['report_feedback'][0]['helpfulness']==5
    assert current['coaching_feedback']==[]


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_action_feedback_still_applies_after_restart(repository, legacy):
    repo = repository
    saved = report(repo)
    if legacy:
        with repo.database.connect() as connection:
            connection.execute("DELETE FROM parent_action_snapshot WHERE report_id=?", (saved["report_id"],))
            connection.commit()
        repo.save_parent_report_feedback(saved["report_id"], False, 5, 5, "未执行")
    else:
        bound_feedback(repo, saved, False, None, None)
    sid = session(repo)
    _, unexecuted = build_daily_report(repo.report_evidence(sid))
    assert unexecuted["adaptation"]["average_effectiveness"] is None
    receipt = (repo.save_parent_report_feedback(saved["report_id"], True, 2, 1, "做过，效果有限") if legacy
               else bound_feedback(repo, saved, revision=1))
    reopened = StudyRepository(Database(repo.database.path))
    evidence = reopened.report_evidence(sid)
    if legacy:
        assert evidence["coaching_feedback"] == []
        assert evidence["legacy_coaching_feedback"][0]["binding_status"] == "unknown"
        _, coaching = build_daily_report(evidence)
        assert coaching["adaptation"]["average_effectiveness"] is None
        return
    assert evidence["coaching_feedback"][0]["feedback_id"] == receipt["feedback_id"]
    assert evidence["coaching_feedback"][0]["feedback_context"] == CONTEXT
    _, coaching = build_daily_report(evidence)
    assert coaching["adaptation"]["average_effectiveness"] == 2
    assert coaching["adaptation"]["feedback_count"] == 1
    assert coaching["action"]["duration_minutes"] == 7
    assert "效果有限" in coaching["adaptation"]["note"]


def test_repository_retains_context_before_selecting_relevant_feedback(repository):
    repo = repository
    matching = report(repo)
    bound_feedback(repo, matching)
    for index in range(6):
        different = report(repo, difficult="听力", subject="英语")
        bound_feedback(repo, different, effectiveness=5, stress_rating=5)
    foreign = report(repo, child=OTHER)
    bound_feedback(repo, foreign, effectiveness=5, stress_rating=5)
    _, coaching = build_daily_report(repo.report_evidence(session(repo)))
    assert coaching["adaptation"]["feedback_count"] == 1
    assert coaching["adaptation"]["average_effectiveness"] == 2


@pytest.mark.parametrize("source,version,expected", [
    ("local_rules_v2", "different_strategy_v1", None),
    ("local_rules_revalidated_v3", "local_rules_v2", 2),
    ("cloud_parent_coaching_v1", "local_rules_v2", None),
])
def test_source_wrapper_cannot_erase_action_strategy_version(repository, source, version, expected):
    repo = repository
    saved = report(repo, source=source, version=version)
    bound_feedback(repo, saved)
    _, next_coaching = build_daily_report(repo.report_evidence(session(repo)))
    assert next_coaching["adaptation"]["average_effectiveness"] == expected


def test_matching_executed_actions_average_without_missing_or_invalid_ratings():
    _, coaching = build_daily_report(material([
        action(effectiveness=1, stress_rating=None),
        action(effectiveness=3, stress_rating=5),
        action(effectiveness=None, stress_rating=1),
        action(effectiveness=True, stress_rating=0),
    ]))
    assert coaching["adaptation"]["average_effectiveness"] == 2
    assert coaching["adaptation"]["average_stress"] == 3
    assert coaching["adaptation"]["feedback_count"] == 3

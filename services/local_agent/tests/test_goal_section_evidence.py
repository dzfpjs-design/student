"""Section scope is not official part coverage, participation or mastery."""
from datetime import date, datetime, timezone
import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db, _qualified_goal_fixture
from test_goal_roadmap import configure

CHILD = 'child-junior-3'
TRACK = 'junior_exam_readiness'
AT = '2026-09-10T06:00:00+00:00'


@pytest.fixture
def section_goal(goal_db, monkeypatch):
    monkeypatch.setattr('home_study_agent.goal_program._now', lambda: AT)
    monkeypatch.setattr('home_study_agent.goal_roadmap._as_of',
        lambda day: (day or date(2026,9,10), datetime.fromisoformat(AT)))
    service = GoalProgramService(goal_db)
    configure(service, child=CHILD, track=TRACK, target_date='2026-09-20')
    return goal_db, service


@pytest.fixture
def exact_goal_item(section_goal, monkeypatch):
    # Source qualification must precede the fixed report time on every run date.
    freeze_supply_time(monkeypatch, AT)
    return _qualified_goal_fixture(section_goal[0])


def freeze_supply_time(monkeypatch, stamp):
    class SupplyClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr('home_study_agent.local_supply.datetime', SupplyClock)
    monkeypatch.setattr('home_study_agent.specialized_supply._now', lambda: stamp)


def view(db, child=CHILD, track=TRACK):
    return GoalRoadmapService(db).read(child, track)['section_evidence']


def section(projection, name):
    return next(s for s in projection['sections'] if s['section_id'] == name)


def answer(db, monkeypatch, item, text, key, *, at=AT, hint=0):
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, 'section-original-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    monkeypatch.setattr('home_study_agent.learning_state.utc_now', lambda: at)
    return LearningStateService(db).submit_diagnostic(sid, item, text, key, hint_level=hint)


def test_actual_qualified_answer_is_not_full_subject_coverage(section_goal, exact_goal_item, monkeypatch):
    db, _ = section_goal
    item, expected = exact_goal_item
    before = section(view(db), '数学')
    assert item in {r['diagnostic_item_id'] for r in before['sources']}
    assert before['opportunity_count'] == before['qualified_evidence_count'] == 0
    receipt = answer(db, monkeypatch, item, expected, 'section-answer')
    after = view(db)
    math = section(after, '数学')
    assert math['opportunity_count'] == math['qualified_evidence_count'] == 1
    assert math['independent_correct_count'] == 1
    assert receipt['evidence_id'] in {r['evidence_id'] for r in math['qualified_evidence']}
    assert math['official_part_mapping'] == 'missing'
    assert not after['exam_readiness_claim_allowed']
    assert view(Database(db.path)) == after


@pytest.mark.parametrize('text,hint,qualified,independent', [('0',0,1,0), ('correct',1,1,0)])
def test_reliable_error_and_supported_answer_do_not_become_independent_success(
        section_goal, exact_goal_item, monkeypatch, text, hint, qualified, independent):
    db, _ = section_goal
    item, expected = exact_goal_item
    answer(db, monkeypatch, item, expected if text == 'correct' else text, 'condition-answer', hint=hint)
    result = section(view(db), '数学')
    assert result['opportunity_count'] == 1
    assert result['qualified_evidence_count'] == qualified
    assert result['independent_correct_count'] == independent


def test_future_answer_does_not_count_and_unrelated_subject_is_not_borrowed(section_goal, exact_goal_item, monkeypatch):
    db, _ = section_goal
    answer(db, monkeypatch, exact_goal_item[0], exact_goal_item[1], 'future-answer', at='2026-09-10T06:00:01+00:00')
    result = view(db)
    assert section(result, '数学')['opportunity_count'] == 0
    assert section(result, '物理')['qualified_evidence_count'] == 0
    assert not result['exam_readiness_claim_allowed']


def test_future_source_qualification_is_not_available_to_earlier_report(section_goal, monkeypatch):
    db, _ = section_goal
    freeze_supply_time(monkeypatch, '2026-09-10T06:00:01+00:00')
    item, _ = _qualified_goal_fixture(db)
    assert item not in {r['diagnostic_item_id'] for r in section(view(db), '数学')['sources']}
    with db.connect() as c:
        assert c.execute('SELECT reviewed_at FROM specialized_supply_candidate WHERE diagnostic_item_id=?',
                         (item,)).fetchone()[0] == '2026-09-10T06:00:01+00:00'


def test_item_revocation_preserves_attempt_but_removes_current_coverage(section_goal, exact_goal_item, monkeypatch):
    db, _ = section_goal
    item, expected = exact_goal_item
    receipt = answer(db, monkeypatch, item, expected, 'answer-before-withdraw')
    assert section(view(db), '数学')['qualified_evidence_count'] == 1
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (item,))
        c.commit()
    result = view(Database(db.path))
    assert section(result, '数学')['qualified_evidence_count'] == 0
    assert result['unmapped_or_unavailable_opportunity_count'] == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE evidence_id=?', (receipt['evidence_id'],)).fetchone()[0] == 1


def test_projection_read_never_writes_and_plan_completions_are_not_answer_opportunities(section_goal):
    from home_study_agent.goal_section_coverage import build_section_evidence
    from home_study_agent.official_goal_rules import target_rule_context
    db, _ = section_goal
    rules = target_rule_context(TRACK, {'target_date':'2026-09-20'},
        as_of_date=date(2026,9,10), curriculum_region='南京')
    with db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        result = build_section_evidence(c, CHILD, rules, as_of=datetime.fromisoformat(AT))
        assert all(s['opportunity_count']==0 for s in result['sections'])
        assert all(s['qualified_evidence_count']==0 for s in result['sections'])
        assert c.total_changes == 0
    # Bound/unbound rule acknowledgement does not alter local evidence authority.
    assert rules['binding_status'] == 'unbound'


def test_same_grade_other_child_source_is_not_disclosed_as_unavailable(section_goal, exact_goal_item):
    from home_study_agent.goal_section_coverage import build_section_evidence
    from home_study_agent.official_goal_rules import target_rule_context
    db, _ = section_goal
    other = 'child-primary-4'
    rules = target_rule_context(TRACK, {'target_date':'2026-09-20'},
        as_of_date=date(2026,9,10), curriculum_region='南京')
    with db.connect() as c:
        c.execute('UPDATE child_profile SET grade=? WHERE child_id=?', ('初三', other))
        c.commit()
        c.execute('BEGIN')
        projection = build_section_evidence(c, other, rules, as_of=datetime.fromisoformat(AT))
    assert exact_goal_item[0] not in json.dumps(projection, ensure_ascii=False)


def test_storage_read_failure_is_not_returned_as_zero_opportunities(section_goal):
    from home_study_agent.goal_section_coverage import build_section_evidence
    from home_study_agent.official_goal_rules import target_rule_context
    db, _ = section_goal
    rules = target_rule_context(TRACK, {'target_date':'2026-09-20'},
        as_of_date=date(2026,9,10), curriculum_region='南京')
    with db.connect() as c:
        c.execute('BEGIN')
        c.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == 'diagnostic_attempt' else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            build_section_evidence(c, CHILD, rules, as_of=datetime.fromisoformat(AT))

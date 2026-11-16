"""Report association uses actual scored attempts, current qualification and owner."""
from datetime import datetime, timedelta
from uuid import uuid4
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.reports import build_daily_report
from test_mistake_gate_reliability import CHILD, ITEM, SECOND, flow


def close(repo, sid):
    repo.transition(sid, 'study', 'reflection', 'Synthetic day closed', str(uuid4()))
    return repo.complete_with_reflection(sid, '', '', 'okay', '', True, str(uuid4()))


@pytest.fixture
def two_days(flow, monkeypatch):
    db, learning, sid, clock = flow
    from home_study_agent import learning_obligations
    class ProjectionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(learning_obligations, "datetime", ProjectionClock)
    repo = StudyRepository(db)
    original = learning.submit_diagnostic(sid, ITEM, '12', str(uuid4()))
    book = MistakeBookService(db)
    case = book.capture_from_attempt(original)['mistake_id']
    book.confirm(case, CHILD, 'confirm', str(uuid4()))
    book.diagnose(case, CHILD, 'unknown', 'Candidate cause, not a mastery fact', .8, str(uuid4()))
    book.add_remediation(case, CHILD, 'active_recall', 'completed', None, str(uuid4()))
    book.submit_attempt(case, CHILD, 'near_transfer', SECOND, '42', 0, str(uuid4()), _session_id=sid)
    close(repo, sid)
    clock[0] += timedelta(hours=49)
    second = repo.start_session(CHILD, str(uuid4()))['session_id']
    with db.connect() as c:
        # Synthetic setup of the next active study phase, not a learning record.
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (second,))
        c.commit()
    return db, repo, book, case, sid, second, clock


def retest(data, answer='42'):
    db, repo, book, case, first, second, clock = data
    key = str(uuid4())
    book.submit_attempt(case, CHILD, 'delayed_48h', SECOND, answer, 0, key, _session_id=second)
    close(repo, second)
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', (key,)).fetchone())
    return row


@pytest.mark.parametrize('answer,status', [('42', 'mastered'), ('12', 'fragile')])
def test_real_retest_association_survives_report_reload_and_rebuild(two_days, answer, status):
    db, repo, book, case, first, second, clock = two_days
    row = retest(two_days, answer)
    evidence = repo.report_evidence(second)
    assert [item['mistake_id'] for item in evidence['mistakes']] == [case]
    assert evidence['mistakes'][0]['status'] == status
    assert all(item['evidence_id'] != row['evidence_id'] for item in repo.report_evidence(first)['knowledge_evidence'])
    assert len(evidence['knowledge_evidence']) == 1
    summary, coaching = build_daily_report(evidence)
    stored = repo.save_daily_report(second, summary, coaching)
    restored = StudyRepository(Database(db.path)).get_daily_report(stored['report_id'])
    assert restored['evidence_detail']['mistakes'] == evidence['mistakes']
    if answer == '12':
        assert any('需继续处理的错题' in fact for fact in restored['summary']['facts'])
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM mistake_attempt WHERE attempt_id=?', (row['attempt_id'],)).fetchone()) == row


@pytest.mark.parametrize('change', ['withdraw_case', 'reference_changed', 'other_child', 'original_owner_changed', 'outside_session_time'])
def test_invalid_or_foreign_retest_cannot_associate_an_older_case(two_days, change):
    db, repo, book, case, first, second, clock = two_days
    row = retest(two_days)
    original = repo.report_evidence(second)
    assert [item['mistake_id'] for item in original['mistakes']] == [case]
    if change == 'withdraw_case':
        book.confirm(case, CHILD, 'exclude', str(uuid4()))
    else:
        with db.connect() as c:
            if change == 'reference_changed':
                c.execute("UPDATE diagnostic_item SET prompt='Changed source text' WHERE diagnostic_item_id=?", (SECOND,))
            elif change == 'original_owner_changed':
                c.execute("UPDATE learning_evidence SET child_id='child-junior-3' WHERE evidence_id=(SELECT source_evidence_id FROM mistake_case WHERE mistake_id=?)", (case,))
            elif change == 'other_child':
                c.execute("UPDATE learning_evidence SET child_id='child-junior-3' WHERE evidence_id=?", (row['evidence_id'],))
            else:
                c.execute('UPDATE mistake_attempt SET created_at=? WHERE attempt_id=?',
                          ((clock[0]+timedelta(days=1)).isoformat(), row['attempt_id']))
            c.commit()
    evidence = repo.report_evidence(second)
    assert evidence['mistakes'] == []
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt WHERE attempt_id=?', (row['attempt_id'],)).fetchone()[0] == 1


def test_a_third_day_without_a_saved_retest_does_not_import_previous_case(two_days):
    db, repo, book, case, first, second, clock = two_days
    retest(two_days, '12')
    clock[0] += timedelta(days=1)
    third = repo.start_session(CHILD, str(uuid4()))['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (third,))
        c.commit()
    close(repo, third)
    assert repo.report_evidence(third)['mistakes'] == []
    assert repo.report_evidence(third)['knowledge_evidence'] == []


@pytest.mark.parametrize('change', ['withdraw_case', 'reference_changed'])
def test_saved_retest_report_refreshes_after_current_source_invalidation(two_days, change):
    db, repo, book, case, first, second, clock = two_days
    row = retest(two_days)
    summary, coaching = build_daily_report(repo.report_evidence(second))
    stored = repo.save_daily_report(second, summary, coaching)
    repo.save_parent_report_feedback(stored['report_id'], True, 2, 1, 'Keep real parent feedback')
    if change == 'withdraw_case':
        book.confirm(case, CHILD, 'exclude', str(uuid4()))
    else:
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET prompt='Changed actual source' WHERE diagnostic_item_id=?", (SECOND,))
            c.commit()
    restored = StudyRepository(Database(db.path)).get_daily_report(stored['report_id'])
    assert restored['evidence_detail']['mistakes'] == []
    assert restored['feedback']['executed'] and restored['feedback']['effectiveness'] == 2
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM mistake_attempt WHERE attempt_id=?', (row['attempt_id'],)).fetchone()) == row

from datetime import datetime, timedelta

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_mistake_gate_reliability import flow, prepare, CHILD, SECOND


@pytest.fixture
def retained_report(flow, monkeypatch):
    from home_study_agent import learning_obligations
    db, _, sid, clock = flow
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(learning_obligations, 'datetime', Clock)
    book, mid = prepare(flow)
    book.submit_attempt(mid, CHILD, 'near_transfer', SECOND, '42', 0, 'parent-baseline', _session_id=sid)
    clock[0] += timedelta(hours=49)
    book.submit_attempt(mid, CHILD, 'delayed_48h', SECOND, '42', 0, 'parent-retention', _session_id=sid)
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, '我先想运算顺序再计算', '', 'okay', '', False, 'retained-end')
    return db, repo, sid, book, mid


def test_retention_yields_concrete_parent_support_and_withdrawal_preserves_history(retained_report):
    db, repo, sid, book, mid = retained_report
    evidence = repo.report_evidence(sid)
    summary, coaching = build_daily_report(evidence)
    assert coaching['focus'] == 'verified_retention_support', coaching['focus']
    target = coaching['action']['learning_target']
    assert target['retention_basis_refs']
    assert target['next_use'] == 'optional_independence_support'
    assert not target['mastery_claim_allowed']
    assert target['knowledge_title'] in coaching['scenario']
    assert len(coaching['script']) >= 4 and len(coaching['avoid']) >= 3
    assert 0 < coaching['action']['duration_minutes'] <= 5
    saved = repo.save_daily_report(sid, summary, coaching)
    reopened = StudyRepository(Database(db.path)).get_daily_report(saved['report_id'])
    assert reopened['parent_action']['snapshot_id'] == saved['parent_action']['snapshot_id']
    assert reopened['parent_coaching'] == coaching
    repo.save_parent_report_feedback(saved['report_id'], executed=True,
        executed_at=saved['parent_action']['created_at'], effectiveness=2, stress_rating=5,
        notes_summary='合成记录：沟通压力偏高', action_snapshot_id=saved['parent_action']['snapshot_id'],
        expected_revision=0, client_action_id='retention-pressure')
    adjusted = build_daily_report(StudyRepository(Database(db.path)).report_evidence(sid))[1]
    assert adjusted['action']['duration_minutes'] == 2
    assert adjusted['adaptation']['feedback_count'] == 1
    book.confirm(mid, CHILD, 'exclude', 'retained-withdraw')
    current = StudyRepository(Database(db.path)).get_daily_report(saved['report_id'])
    assert current['parent_coaching']['focus'] != 'verified_retention_support'
    old = ParentActionEvidenceService(Database(db.path)).get(CHILD, saved['parent_action']['snapshot_id'])
    assert old['coaching']['action']['learning_target'] == target


@pytest.mark.parametrize('priority', ['skip', 'empty_day', 'next_action', 'difficulty', 'unqualified'])
def test_retention_never_overrides_child_choice_or_unverified_material(retained_report, priority):
    _, repo, sid, _, _ = retained_report
    evidence = repo.report_evidence(sid)
    if priority == 'skip':
        evidence['reflection']['skipped'] = True
    elif priority == 'empty_day':
        evidence['session']['homework_declared_empty'] = True
    elif priority == 'next_action':
        evidence['reflection']['next_action'] = '明天先整理书包'
    elif priority == 'difficulty':
        evidence['reflection']['difficult_text'] = '今天有点累，想先休息'
    else:
        evidence['reliable_learning']['items'] = []
    coaching = build_daily_report(evidence)[1]
    assert coaching['focus'] != 'verified_retention_support'
    assert coaching.get('action', {}).get('learning_target', {}).get('next_use') != 'optional_independence_support'


@pytest.mark.parametrize('scope', ['valid', 'unbound', 'other_child', 'not_studying', 'closed'])
def test_new_attempt_binds_only_its_actual_active_learning_session(flow, scope):
    from home_study_agent.mistake_book import InvalidMistakeInputError
    db, _, sid, _ = flow
    book, mid = prepare(flow)
    requested = sid
    if scope == 'other_child':
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='completed',status='completed' WHERE session_id=?", (sid,))
            c.commit()
        requested = StudyRepository(db).start_session('child-junior-3', 'other-session')['session_id']
    elif scope in {'not_studying', 'closed'}:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state=?,status=? WHERE session_id=?",
                ('reflection' if scope == 'not_studying' else 'completed', 'active' if scope == 'not_studying' else 'completed', sid))
            c.commit()
    elif scope == 'unbound':
        requested = None
    if scope not in {'valid', 'unbound'}:
        with pytest.raises(InvalidMistakeInputError):
            book.submit_attempt(mid, CHILD, 'near_transfer', SECOND, '42', 0, 'session-check', _session_id=requested)
    else:
        book.submit_attempt(mid, CHILD, 'near_transfer', SECOND, '42', 0, 'session-check', _session_id=requested)
    with db.connect() as c:
        rows = c.execute('SELECT session_id FROM learning_evidence WHERE evidence_id IN (SELECT evidence_id FROM mistake_attempt WHERE mistake_id=?)', (mid,)).fetchall()
        assert [r[0] for r in rows] == ([requested] if scope in {'valid', 'unbound'} else [])


def test_legacy_unbound_retry_never_moves_history_into_current_report(flow):
    db, _, sid, _ = flow
    book, mid = prepare(flow)
    book.submit_attempt(mid, CHILD, 'near_transfer', SECOND, '42', 0, 'legacy-unbound')
    book.submit_attempt(mid, CHILD, 'near_transfer', SECOND, '42', 0, 'legacy-unbound', _session_id=sid)
    with db.connect() as c:
        rows = c.execute('SELECT session_id FROM learning_evidence WHERE evidence_id IN (SELECT evidence_id FROM mistake_attempt WHERE mistake_id=?)', (mid,)).fetchall()
        assert [r[0] for r in rows] == [None]

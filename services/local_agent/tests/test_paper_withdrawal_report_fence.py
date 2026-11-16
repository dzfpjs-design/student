"""Actual paper reconciliation must serialize only withdrawal, not new work."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

from home_study_agent.database import ExternalEffectBusyError
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.repository import StudyRepository, utc_now
from test_paper_scoring_dialogue_reconciliation import pending_dialogue, qualify


def _case(tmp_path, *, stale):
    database, scoring, request = pending_dialogue(tmp_path)
    decision = qualify(scoring, request, '80')
    assert MistakeDialogueService(database).reconcile_pending_scoring()[0]['outcome'] == 'correct_verification_scheduled'
    with database.connect() as c:
        evidence = c.execute('SELECT source_evidence_id FROM paper_correct_verification_obligation WHERE decision_id=?',
                             (decision['decision_id'],)).fetchone()[0]
        if stale:
            c.execute('INSERT INTO paper_scoring_decision_status_event VALUES(?,?,?,?,?,?,?)',
                      ('test-stale', decision['decision_id'], 'child', 'withdrawn', 'synthetic authority change', utc_now(), 'test-stale'))
        c.execute('INSERT OR IGNORE INTO family_rule(child_id,max_learning_minutes,task_segment_minutes,break_minutes,pet_min_minutes,competition_minutes,version,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                  ('child', 120, 25, 5, 0, 0, 1, utc_now()))
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='study'")
        c.commit()
    repo = StudyRepository(database)
    session = repo.start_session('child', 'new-study')['session_id']
    with database.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (session,))
        c.commit()
    return database, repo, session, evidence


def _hold_guard(database):
    acquired, release = Event(), Event()
    def run():
        with database.external_effect_guard(timeout=5):
            acquired.set()
            release.wait(20)
    thread = Thread(target=run, daemon=True)
    thread.start()
    assert acquired.wait(5)
    return release, thread


def test_generate_plan_rolls_back_before_guard_retry(tmp_path, monkeypatch):
    from home_study_agent import learning_obligations
    database, repo, session, evidence = _case(tmp_path, stale=True)
    original = learning_obligations.withdraw_excluded_reviews
    def prior_write(c, child, now):
        c.execute("INSERT OR IGNORE INTO audit_event VALUES('synthetic-probe','system','synthetic-probe',?, '{}',?)", (child, now))
        return original(c, child, now)
    monkeypatch.setattr(learning_obligations, 'withdraw_excluded_reviews', prior_write)
    release, thread = _hold_guard(database)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ExternalEffectBusyError):
                pool.submit(repo.generate_plan, session, 'plan-after-withdrawal').result(timeout=12)
        with database.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='synthetic-probe'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal WHERE evidence_id=?', (evidence,)).fetchone()[0] == 0
    finally:
        release.set(); thread.join(5)
    repo.generate_plan(session, 'plan-after-withdrawal')
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='synthetic-probe'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal WHERE evidence_id=?', (evidence,)).fetchone()[0] == 1


def test_generate_plan_without_withdrawal_does_not_wait_for_sender(tmp_path):
    database, repo, session, evidence = _case(tmp_path, stale=False)
    release, thread = _hold_guard(database)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(repo.generate_plan, session, 'normal-plan').result(timeout=12)
        assert result['session']['session_id'] == session
        with database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal WHERE evidence_id=?', (evidence,)).fetchone()[0] == 0
    finally:
        release.set(); thread.join(5)


def test_dialogue_reconciliation_rolls_back_and_recovers_when_guard_busy(tmp_path, monkeypatch):
    from home_study_agent import paper_correct_revision_verification as revision
    from home_study_agent.paper_scoring import _require_reconciliation_guard_if_probing
    import test_paper_scoring_dialogue_reconciliation as journeys
    original_case = journeys.pending_dialogue
    original_resume = revision.resume_pending_supply
    captured = {}
    def capture_case(path):
        case = original_case(path)
        captured['database'] = case[0]
        return case
    def fence_before_resume(c, child):
        if not captured.get('injected'):
            captured['injected'] = True
            c.execute("INSERT INTO audit_event VALUES('dialogue-probe','system','dialogue-probe',?, '{}',?)", (child, utc_now()))
            captured['release'], captured['thread'] = _hold_guard(captured['database'])
            _require_reconciliation_guard_if_probing()
        return original_resume(c, child)
    monkeypatch.setattr(journeys, 'pending_dialogue', capture_case)
    monkeypatch.setattr(revision, 'resume_pending_supply', fence_before_resume)
    try:
        with pytest.raises(ExternalEffectBusyError):
            journeys.test_correct_revision_schedules_qualified_distinct_item_only_with_receipt(tmp_path)
        database = captured['database']
        with database.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='dialogue-probe'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM paper_scoring_dialogue_revision_reconciliation').fetchone()[0] == 0
    finally:
        captured['release'].set(); captured['thread'].join(5)
    result = MistakeDialogueService(database).reconcile_pending_scoring()
    assert len(result) == 1 and result[0]['outcome'] == 'correct_verification_scheduled'
    assert MistakeDialogueService(database).reconcile_pending_scoring() == []

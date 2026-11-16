"""Malformed historical ownership imported through the real sidecar migration."""
import pytest
import hashlib
import sqlite3

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from test_behavior_observation_coverage import managed

A = 'child-primary-4'
B = 'child-junior-3'
DAY = '2026-08-25'


def old_history(tmp_path):
    service = BehaviorModelService(tmp_path / 'legacy-owner.db')
    for child, kind in [(A, 'work.reading'), (B, 'fatigue.candidate')]:
        service.ingest(ObservableEvent(child_id=child, session_id='session-' + child,
            event_id='source-' + child, occurred_at=DAY + 'T10:00:00Z', event_type=kind,
            source='local_vision', confidence=1))
    decision = service.decide_intervention(B, 'session-' + B, DAY + 'T10:01:00Z')
    assert decision['status'] == 'suggested', decision
    service.record_feedback(decision['intervention_id'], True, DAY + 'T10:02:00Z',
        child_id=B, session_id='session-' + B, client_action_id='feedback-b')
    return service, decision['intervention_id']


@pytest.mark.parametrize('damage', ['state_event_owner', 'feedback_source_owner'])
def test_imported_cross_owner_record_is_not_published_in_other_child_period_context(managed, tmp_path, damage):
    old, intervention = old_history(tmp_path)
    with old._connect() as c:
        if damage == 'state_event_owner':
            # State remains B-owned, but an old repair linked it to A's event.
            c.execute('UPDATE probabilistic_state SET event_id=? WHERE child_id=?', ('source-' + A, B))
        else:
            # Receipt claims A; its session and original source still belong to B.
            c.execute('UPDATE intervention SET child_id=? WHERE intervention_id=?', (A, intervention))
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    imported = BehaviorModelService(managed[0], legacy_path=old.path)
    imported.initialize()
    context = imported.report_safe_context(A, period_start=DAY, period_end=DAY)
    if damage == 'state_event_owner':
        assert context['behavior_model_summary']['active_hypotheses'] == []
    else:
        assert context['behavior_intervention_feedback'] == []
    assert context['behavior_model_summary']['excluded_history_issues']
    with imported._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM probabilistic_state').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='behavior_legacy_owner_links_excluded'").fetchone()[0] >= 1
    assert old.path.with_name(old.path.name + '.migrated').is_file()
    reopened = BehaviorModelService(managed[0])
    assert reopened.report_safe_context(A, period_start=DAY, period_end=DAY) == context
    daily = reopened.report_safe_context(A, 'session-' + A)
    assert daily['behavior_model_summary']['active_hypotheses'] == []
    assert daily['behavior_intervention_feedback'] == []


def test_undamaged_double_child_import_stays_isolated(managed, tmp_path):
    old, _ = old_history(tmp_path)
    imported = BehaviorModelService(managed[0], legacy_path=old.path)
    imported.initialize()
    a = imported.report_safe_context(A, period_start=DAY, period_end=DAY)
    b = imported.report_safe_context(B, period_start=DAY, period_end=DAY)
    assert a['behavior_model_summary']['active_hypotheses'] == []
    assert a['behavior_intervention_feedback'] == []
    assert len(b['behavior_model_summary']['active_hypotheses']) == 1
    assert len(b['behavior_intervention_feedback']) == 1
    assert a['behavior_model_summary'].get('excluded_history_issues', {}) == {}
    assert b['behavior_model_summary'].get('excluded_history_issues', {}) == {}


@pytest.mark.parametrize('damage', ['state_session', 'feedback_session', 'feedback_missing_source'])
def test_history_session_mismatch_is_excluded_without_destroying_originals(tmp_path, damage):
    service, intervention = old_history(tmp_path)
    with service._connect() as c:
        if damage == 'state_session':
            c.execute("UPDATE probabilistic_state SET session_id='different' WHERE child_id=?", (B,))
        elif damage == 'feedback_session':
            c.execute("UPDATE intervention SET session_id='different' WHERE intervention_id=?", (intervention,))
        else:
            c.execute("UPDATE intervention SET source_event_id='missing' WHERE intervention_id=?", (intervention,))
        c.commit()
    context = service.report_safe_context(B, period_start=DAY, period_end=DAY)
    replay = service.replay(B)
    if damage == 'state_session':
        assert context['behavior_model_summary']['active_hypotheses'] == []
        assert replay['states'] == []
    else:
        assert context['behavior_intervention_feedback'] == []
        assert replay['interventions'] == []
        sid = 'different' if damage == 'feedback_session' else 'session-' + B
        assert service.feedback_for_action('feedback-b', B, sid) is None
    assert context['behavior_model_summary']['excluded_history_issues']
    with service._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM observable_event').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 1


def test_sidecar_owner_issue_audit_and_import_rollback_together(managed, tmp_path):
    old, intervention = old_history(tmp_path)
    with old._connect() as c:
        c.execute('UPDATE intervention SET child_id=? WHERE intervention_id=?', (A, intervention))
        c.commit()
    digest = hashlib.sha256(old.path.read_bytes()).hexdigest()
    db = managed[0]
    with db.connect() as c:
        c.execute("""CREATE TRIGGER reject_owner_audit BEFORE INSERT ON audit_event
            WHEN NEW.action='behavior_legacy_owner_links_excluded'
            BEGIN SELECT RAISE(ABORT,'synthetic audit interruption'); END""")
        c.commit()
    service = BehaviorModelService(db, legacy_path=old.path)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic audit interruption'):
        service.initialize()
    assert old.path.is_file()
    assert hashlib.sha256(old.path.read_bytes()).hexdigest() == digest
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM observable_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM behavior_legacy_migration').fetchone()[0] == 0
        c.execute('DROP TRIGGER reject_owner_audit')
        c.commit()
    service.initialize()
    service.initialize()
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM behavior_legacy_migration').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='behavior_legacy_owner_links_excluded'").fetchone()[0] == 2
        assert c.total_changes == 0
    assert service.report_safe_context(A)['behavior_intervention_feedback'] == []

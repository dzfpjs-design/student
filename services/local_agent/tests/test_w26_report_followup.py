from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_followup import ReportFollowupService, ReportFollowupConflict
from test_daily_report_evidence_versions import CHILD, flow, retire_source


def setup_feedback(flow):
    db, repo, report, delivery = flow
    service = ParentReportService(db)
    snapshot = service.create_from_daily(report)
    service.approve(snapshot['snapshot_id'])
    service.record_feedback(snapshot['snapshot_id'], 5, 4, 'Useful, but not attempted', 'feedback')
    return service, snapshot, ReportFollowupService(db)


def plan(db, child=CHILD, day=None, key='next-plan'):
    day = day or (date.today() + timedelta(days=1)).isoformat()
    with db.connect() as c:
        sid = c.execute('SELECT session_id FROM daily_session WHERE child_id=? LIMIT 1', (child,)).fetchone()[0]
        c.execute("INSERT INTO daily_plan VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (key, child, sid, day, 'ready', 0, 'local_rules', 1, '2099-01-01T00:00:00+00:00', key))
        c.commit()
    return day


def test_saved_feedback_is_versioned_plan_input_not_execution_and_survives_restart(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    inputs = service.project_for_plan(CHILD, day)
    assert inputs['items'][0]['evidence_kind'] == 'report_experience'
    assert inputs['items'][0]['execution_verified'] is False
    assert inputs['items'][0]['learning_effect_verified'] is False
    assert inputs['items'][0]['revision'] == 1
    assert ReportFollowupService(Database(flow[0].path)).project_for_plan(CHILD, day) == inputs
    receipt = service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'read-plan')
    assert receipt['status'] == 'read_only'
    assert receipt['plan_adjustment_verified'] is False
    assert service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'read-plan')['receipt_id'] == receipt['receipt_id']
    assert service.project_for_plan('child-junior-3', day)['items'] == []
    with pytest.raises(ReportFollowupConflict):
        service.record_plan_read('child-junior-3', 'next-plan', inputs['fingerprint'], 'cross-child')


def test_revision_retraction_concurrency_and_stale_receipt(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    old = service.project_for_plan(CHILD, day)
    receipt = service.record_plan_read(CHILD, 'next-plan', old['fingerprint'], 'read')
    def retract(key):
        try:
            return service.retract_feedback(CHILD, snapshot['snapshot_id'], 1, key, 'Misclicked')['revision']
        except ReportFollowupConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(retract, ['retract-a', 'retract-b']))
    assert sorted(map(str, results)) == ['2', 'conflict']
    assert service.project_for_plan(CHILD, day)['items'] == []
    assert service.get_plan_receipt(CHILD, receipt['receipt_id'])['inputs_current'] is False
    with pytest.raises(ReportFollowupConflict):
        service.record_plan_read(CHILD, 'next-plan', old['fingerprint'], 'stale')
    book.record_feedback(snapshot['snapshot_id'], 2, None, 'Revised', 'revised-feedback')
    assert service.project_for_plan(CHILD, day)['items'][0]['revision'] == 3
    assert len(service.feedback_history(CHILD, snapshot['snapshot_id'])) == 3


def test_borrowed_plan_transaction_rolls_back_receipt_and_privacy_delete_is_allowed(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    inputs = service.project_for_plan(CHILD, day)
    with flow[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        receipt = service.record_plan_read(CHILD, 'next-plan', inputs['fingerprint'], 'rolled-back', connection=c)
        c.rollback()
    with pytest.raises(KeyError):
        service.get_plan_receipt(CHILD, receipt['receipt_id'])
    with flow[0].connect() as c:
        c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],))
        assert c.execute('SELECT COUNT(*) FROM report_feedback_revision').fetchone()[0] == 0
        c.commit()


def test_revoked_source_excludes_plan_feedback_but_keeps_history(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    book.revoke(snapshot['snapshot_id'], reason='Source withdrawn')
    assert service.project_for_plan(CHILD, day)['items'] == []
    assert len(service.feedback_history(CHILD, snapshot['snapshot_id'])) == 1


def test_read_projection_excludes_withdrawn_source_without_refresh_or_writes(flow):
    from test_daily_report_evidence_versions import ITEM
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    with flow[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        c.execute('PRAGMA query_only=ON')
        assert service.project_for_plan(CHILD, day, connection=c)['items'] == []
    assert book.get(snapshot['snapshot_id'])['status'] == 'approved'


@pytest.mark.parametrize('dependency', ['mistake_mastery_fingerprint','specialized_reference_fingerprint'])
def test_readonly_qualification_matches_all_repository_revalidation_dependencies(flow, dependency):
    db, repo, report, delivery = flow
    with db.connect() as c:
        evidence = dict(report['evidence'])
        evidence[dependency] = 'obsolete-version'
        c.execute('UPDATE daily_report SET evidence_json=? WHERE report_id=?', (json.dumps(evidence),report['report_id']))
        c.commit()
        c.execute('PRAGMA query_only=ON')
        assert ParentReportService.source_qualification_fingerprint(c, CHILD, 'daily_report', report['report_id']) is None


def test_initialization_never_resurrects_retracted_or_superseded_legacy_feedback(flow):
    book, snapshot, service = setup_feedback(flow)
    day = plan(flow[0])
    with flow[0].connect() as c:
        c.execute('DELETE FROM report_feedback_revision')
        c.commit()
    service.initialize()
    assert len(service.project_for_plan(CHILD, day)['items']) == 1
    service.retract_feedback(CHILD, snapshot['snapshot_id'], 1, 'retract', 'Correcting old input')
    service.initialize()
    assert service.project_for_plan(CHILD, day)['items'] == []
    assert len(service.feedback_history(CHILD, snapshot['snapshot_id'])) == 2


def test_snapshot_current_feedback_never_prefills_retracted_history(flow):
    book, snapshot, service = setup_feedback(flow)
    current = book.get(snapshot['snapshot_id'])
    assert current['feedback_current']['helpfulness'] == 5
    assert current['feedback_current_revision'] == 1
    service.retract_feedback(CHILD, snapshot['snapshot_id'], 1, 'retract', 'Mistaken feedback')
    current = ParentReportService(flow[0]).get(snapshot['snapshot_id'])
    assert current['feedback_current'] is None
    assert current['feedback_current_status'] == 'retracted'
    assert current['feedback_current_revision'] == 2
    assert current['feedback'][0]['helpfulness'] == 5  # History only, not form defaults.
    book.record_feedback(snapshot['snapshot_id'], 2, None, 'Fresh explicit revision', 'new-feedback')
    current = book.get(snapshot['snapshot_id'])
    assert current['feedback_current']['helpfulness'] == 2
    assert current['feedback_current_revision'] == 3


@pytest.mark.parametrize('executed', [True, False, None])
def test_action_feedback_separate_from_report_usefulness_and_withdrawal_downgrades(flow, executed):
    from home_study_agent.parent_action_evidence import ParentActionEvidenceService
    book, snapshot, service = setup_feedback(flow)
    db, repo, report, delivery = flow
    action_service = ParentActionEvidenceService(db)
    action = repo.get_daily_report(report['report_id'])['parent_action']
    assert action['snapshot_id'] != report['parent_action']['snapshot_id']
    action_service.record_feedback(child_id=CHILD, report_id=report['report_id'], snapshot_id=action['snapshot_id'],
        client_action_id='action-feedback', expected_revision=0, executed=executed,
        executed_at=action['created_at'] if executed is True else None,
        effectiveness=2 if executed is True else None, stress_rating=None)
    day = plan(db)
    items = service.project_for_plan(CHILD, day)['items']
    assert len(items) == 2
    assert items[0]['evidence_kind'] == 'report_experience'
    assert items[1]['executed'] is executed
    assert items[1]['learning_effect_verified'] is False
    retire_source(db, repo, report)
    assert all(item['evidence_kind'] != 'parent_reported_action' for item in service.project_for_plan(CHILD, day)['items'])


@pytest.mark.parametrize('delivery_status', ['sent', 'unknown'])
def test_withdrawal_creates_reviewable_correction_without_sending_or_losing_old_submission(flow, delivery_status):
    book, snapshot, service = setup_feedback(flow)
    delivery = flow[3]
    recipient = delivery.add_recipient(CHILD, 'email', 'synthetic@example.invalid', 'Synthetic', True)
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    with flow[0].connect() as c:
        c.execute('UPDATE report_delivery_outbox SET status=? WHERE delivery_id=?', (delivery_status, queued['delivery_id']))
        c.commit()
    # Synthetic stored transport state; this test does not claim actual external delivery.
    retire_source(flow[0], flow[1], flow[2])
    refreshed = book.refresh_sources(CHILD)
    assert refreshed[0]['status'] == 'draft'
    status = book.correction_status(CHILD, snapshot['snapshot_id'])
    assert status['correction_notification_required'] is True
    assert status['automatic_delivery'] is False
    assert status['prior_submissions'][0]['status'] == delivery_status
    assert status['replacements'][0]['replacement_snapshot_id'] == refreshed[0]['snapshot_id']
    assert delivery.process_due() == []
    assert ParentReportService(Database(flow[0].path)).correction_status(CHILD, snapshot['snapshot_id']) == status
    with pytest.raises(KeyError):
        book.correction_status('child-junior-3', snapshot['snapshot_id'])

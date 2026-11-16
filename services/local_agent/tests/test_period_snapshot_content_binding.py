"""A second real study session cannot lend authority to an earlier period draft."""
import pytest

from home_study_agent.parent_reports import SnapshotStateError
from home_study_agent.period_reports import PeriodReportService, PeriodReportSourceError
from home_study_agent.reports import build_daily_report
from test_daily_report_evidence_versions import flow, CHILD


def test_new_session_aggregate_cannot_fresh_stamp_an_earlier_period_copy(flow):
    db, repo, daily, delivery = flow
    periods = PeriodReportService(db)
    old = periods.generate(CHILD, 'weekly')
    sid = repo.start_session(CHILD, 'second-real-report-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, 'Checked a second task', '', 'okay', '', False, 'second-real-report-finish')
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    repo.save_daily_report(sid, summary, coaching)
    fresh = periods.generate(CHILD, 'weekly')
    assert fresh['content']['coverage']['recorded_sessions'] > old['content']['coverage']['recorded_sessions']
    with pytest.raises(SnapshotStateError):
        delivery.snapshots.create_from_period(old)
    assert delivery.snapshots.create_from_period(fresh)['status'] == 'draft'


def test_missing_source_history_is_revoked_before_first_read_and_cannot_supersede(flow, monkeypatch):
    db, _, _, delivery = flow
    parents = delivery.snapshots
    original_get = parents.get
    observed = []

    def checked_get(snapshot_id):
        with db.connect() as c:
            row = c.execute('SELECT status,revoked_at,revoke_reason FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            assert row['status'] == 'revoked'
            assert row['revoked_at'] and row['revoke_reason']
            observed.append(snapshot_id)
        return original_get(snapshot_id)

    def forbid_supersession(*args, **kwargs):
        pytest.fail('Unqualified historical copy cannot supersede current reports')

    monkeypatch.setattr(parents, 'get', checked_get)
    monkeypatch.setattr(parents, '_supersede_period_drafts', forbid_supersession)
    previous = parents.create_from_period({'period_report_id': 'missing-legacy-source',
        'child_id': CHILD, 'report_type': 'weekly', 'period_end': '2026-08-30',
        'source': 'legacy', 'content': {'headline': 'Historical text', 'coverage': {}}})
    assert observed and previous['status'] == 'revoked'
    with pytest.raises(SnapshotStateError):
        parents.approve(previous['snapshot_id'])


def test_unrebuilt_period_cannot_borrow_new_daily_source_authority(flow):
    db, repo, _, delivery = flow
    periods = PeriodReportService(db)
    old = periods.generate(CHILD, 'weekly')
    sid = repo.start_session(CHILD, 'new-unaggregated-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, 'A new completed task', '', 'okay', '', False, 'new-unaggregated-finish')
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    repo.save_daily_report(sid, summary, coaching)
    # The stored period and caller copy still agree, but their sources do not.
    with pytest.raises(SnapshotStateError):
        delivery.snapshots.create_from_period(old)
    fresh = periods.get(old['period_report_id'], persist_refresh=True)
    assert fresh['content']['coverage']['recorded_sessions'] == old['content']['coverage']['recorded_sessions'] + 1
    assert delivery.snapshots.create_from_period(fresh)['status'] == 'draft'


@pytest.mark.parametrize('operation', ['generate', 'refresh'])
def test_learning_changes_between_aggregate_and_writer_preserve_previous_report(flow, monkeypatch, operation):
    db, repo, daily, _ = flow
    periods = PeriodReportService(db)
    previous = periods.generate(CHILD, 'weekly')
    with db.connect() as c:
        original = c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
            (previous['period_report_id'],)).fetchone()[0]
    build = periods._build_content

    def correct_after_aggregate(*args, **kwargs):
        result = build(*args, **kwargs)
        from test_daily_report_evidence_versions import retire_source
        retire_source(db, repo, daily)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(periods, '_build_content', correct_after_aggregate)
        with pytest.raises(PeriodReportSourceError, match='PERIOD_LEARNING_SOURCE_CHANGED'):
            if operation == 'generate':
                periods.generate(CHILD, 'weekly')
            else:
                periods.get(previous['period_report_id'], persist_refresh=True)
    with db.connect() as c:
        assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
            (previous['period_report_id'],)).fetchone()[0] == original
    fresh = periods.get(previous['period_report_id'], persist_refresh=True)
    assert fresh['content']['learning_source_fingerprint'] != previous['content']['learning_source_fingerprint']

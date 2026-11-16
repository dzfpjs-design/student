"""A restored evidence set is a new review, not revival of a revoked snapshot."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_period_report_reliable_evidence import flow, answer, report, CHILD
from test_daily_learning_closure_audit import learning_clock


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_restore_identical_learning_content_creates_new_reviewable_version(flow, kind):
    f = flow
    answer(f, '90')
    parents = ParentReportService(f.db)
    original = parents.create_from_period(report(f, kind))
    parents.edit_draft(original['snapshot_id'], parent_note='Keep the original parent review')
    parents.approve(original['snapshot_id'])
    with f.db.connect() as c:
        learning_before = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (original['snapshot_id'],)).fetchone())
    ref = original['facts']['learning_summary']['evidence'][0]['evidence_ref']
    assert f.reports.retract_evidence(CHILD, ref, '核对来源', 'retract-first')
    missing = parents.create_from_period(report(f, kind))
    assert missing['snapshot_id'] != original['snapshot_id']
    assert parents.get(original['snapshot_id'])['status'] == 'revoked'
    assert f.reports.restore_evidence(CHILD, ref, '已经核对', 'restore-first')
    restored = parents.create_from_period(report(f, kind))
    assert restored['snapshot_id'] not in {original['snapshot_id'], missing['snapshot_id']}
    assert restored['status'] == 'draft' and restored['approved_at'] is None
    old_stamp = original['content']['summary']['learning_source_fingerprint']
    assert restored['content']['summary']['learning_source_fingerprint'] != old_stamp
    assert restored['source_version'] != original['source_version']
    assert restored['evidence_version'] != original['evidence_version']
    # Retraction/restoration changes provenance, not the recovered learning or wording.
    assert {**restored['content'], 'summary': {
        **restored['content']['summary'], 'learning_source_fingerprint': old_stamp,
    }} == original['content']
    assert restored['facts']['learning_summary'] == original['facts']['learning_summary']
    assert parents.get(original['snapshot_id'])['status'] == 'revoked'
    assert parents.get(missing['snapshot_id'])['status'] == 'revoked'
    restarted = PeriodReportService(Database(f.db.path))
    assert restarted.restore_evidence(CHILD, ref, '已经核对', 'restore-first')
    assert ParentReportService(Database(f.db.path)).create_from_period(report(f, kind))['snapshot_id'] == restored['snapshot_id']
    with f.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (restored['snapshot_id'],)).fetchone()
        assert parents.snapshot_source_current(c, row)
        assert c.total_changes == before
        assert c.execute('SELECT COUNT(*) FROM period_report_evidence_action').fetchone()[0] == 2
        assert '已经核对' not in json.dumps(restored['facts'])
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == learning_before
        old = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (original['snapshot_id'],)).fetchone()
        assert old['content_json'] == frozen['content_json']
        assert old['parent_edit_json'] == frozen['parent_edit_json']
    assert not parents.validate_current_for_delivery(restored['snapshot_id'])
    parents.approve(restored['snapshot_id'])
    assert ParentReportService(Database(f.db.path)).validate_current_for_delivery(restored['snapshot_id'])
    assert restarted.retract_evidence(CHILD, ref, '再核对', 'retract-second')
    again = parents.create_from_period(report(f, kind))
    assert again['snapshot_id'] not in {original['snapshot_id'], missing['snapshot_id'], restored['snapshot_id']}
    assert again['status'] == 'draft'

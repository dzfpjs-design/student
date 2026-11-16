from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.report_followup import ReportFollowupService
from datetime import date, timedelta
from test_period_report_reliable_evidence import CHILD, ITEM, SECOND, answer, flow, learning_clock, report
from test_period_adjustment_rebinding_http import journey


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_period_send_rechecks_withdrawn_source_without_rewriting_approved_copy(flow, kind):
    answer(flow)
    service = ParentReportService(flow.db)
    snapshot = service.create_from_period(report(flow, kind))
    service.approve(snapshot['snapshot_id'])
    assert service.validate_current_for_delivery(snapshot['snapshot_id'])
    with flow.db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        connection.commit()
    assert not service.validate_current_for_delivery(snapshot['snapshot_id'])
    old = service.get(snapshot['snapshot_id'])
    assert old['status'] == 'revoked'
    assert old['content'] == snapshot['content']
    replacement = service.create_from_period(flow.reports.get(report(flow, kind)['period_report_id']))
    assert replacement['snapshot_id'] != old['snapshot_id']
    assert replacement['status'] == 'draft'


@pytest.mark.parametrize('field,value', [
    ('headline', 'Revised source headline'),
    ('interpretation', 'Revised source interpretation; no mastery claim'),
    ('parent_script', ['A revised question for the parent']),
    ('next_action', {'what': 'Review the original together'}),
])
def test_period_version_covers_every_frozen_content_field(flow, monkeypatch, field, value):
    service = ParentReportService(flow.db)
    source = report(flow)
    old = service.create_from_period(source)
    changed = deepcopy(source)
    changed['content'][field] = value
    with pytest.raises(SnapshotStateError):
        service.create_from_period(changed)
    assert service.get(old['snapshot_id'])['status'] == 'draft'
    # Only generator wording is synthetic; persist through the real guarded writer.
    build = PeriodReportService._build_content
    monkeypatch.setattr(PeriodReportService, '_build_content', lambda *args, **kwargs: {
        **build(*args, **kwargs), field: value})
    persisted = report(flow)
    new = service.create_from_period(persisted)
    assert new['snapshot_id'] != old['snapshot_id']
    assert new['content']['summary'][field] == value
    assert new['facts']['learning_summary'] == old['facts']['learning_summary']
    assert service.get(old['snapshot_id'])['content'] == old['content']
    reopened = ParentReportService(Database(flow.db.path))
    assert reopened.get(new['snapshot_id'])['content'] == new['content']
    assert not reopened.validate_current_for_delivery(new['snapshot_id'])
    reopened.approve(new['snapshot_id'])
    assert reopened.validate_current_for_delivery(new['snapshot_id'])


def test_concurrent_period_versions_have_one_current_draft_and_old_replay_cannot_revoke_it(flow):
    service = ParentReportService(flow.db)
    source = report(flow)
    original = service.create_from_period(source)
    service.edit_draft(original['snapshot_id'], parent_note='Keep my earlier review')
    service.approve(original['snapshot_id'])
    answer(flow)
    first = report(flow)
    first_snapshot = service.create_from_period(first)
    answer(flow, '12', item=SECOND)
    current = report(flow)
    def freeze(candidate):
        try:
            return ParentReportService(Database(flow.db.path)).create_from_period(candidate)
        except SnapshotStateError:
            return None
    with ThreadPoolExecutor(max_workers=3) as executor:
        stale, one, two = list(executor.map(freeze, [first, current, current]))
    assert stale is None
    assert one['snapshot_id'] == two['snapshot_id']
    with pytest.raises(SnapshotStateError):
        service.create_from_period(source)
    with flow.db.connect() as c:
        active = c.execute("SELECT snapshot_id FROM parent_report_snapshot WHERE child_id=? AND status!='revoked'", (CHILD,)).fetchall()
    assert len(active) == 1
    assert active[0][0] == one['snapshot_id']
    old = service.get(original['snapshot_id'])
    assert old['status'] == 'revoked' and old['content'] == original['content']
    assert old['parent_edit']['parent_note'] == 'Keep my earlier review'
    assert service.get(first_snapshot['snapshot_id'])['status'] == 'revoked'
    restarted = ParentReportService(Database(flow.db.path))
    assert restarted.create_from_period(current)['snapshot_id'] == one['snapshot_id']
    assert restarted.get(one['snapshot_id'])['status'] == 'draft'


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_period_feedback_projection_is_readonly_and_rechecks_unrefreshed_source(flow, kind, journey):
    from datetime import datetime
    from home_study_agent.report_followup import ReportFollowupConflict
    from test_period_feedback_explicit_contract import bind, durable_rows
    _, _, client, headers = journey
    answer(flow)
    book = ParentReportService(flow.db)
    snapshot = book.create_from_period(report(flow, kind))
    book.approve(snapshot['snapshot_id'])
    book.record_feedback(snapshot['snapshot_id'], 5, None, 'Synthetic useful report', 'feedback')
    service = ReportFollowupService(flow.db)
    day = (datetime.fromisoformat(flow.clock[0]).date() + timedelta(days=1)).isoformat()
    # Rating A changes its source: preserve its witness, never infer current authority.
    with flow.db.connect() as c:
        before = durable_rows(c)
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        raw = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()
        assert raw['status'] == 'approved'
        assert service.current_feedback(c, raw) == {'feedback_current': None,
            'feedback_current_revision': 1, 'feedback_current_status': 'source_invalid'}
        history = service.coaching_experiences(c, CHILD)
        assert len(history) == 1 and history[0]['snapshot_id'] == snapshot['snapshot_id']
        assert history[0]['revision'] == 1 and history[0]['approval_ref']
        projected = service.project_for_plan(CHILD, day, connection=c)
        assert projected['items'] == [] and projected['constraints'] == []
        assert c.total_changes == 0 and durable_rows(c) == before
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        service.propose_plan_adjustment(CHILD, snapshot['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'forbidden-old-implicit-cap')
    with flow.db.connect() as c:
        assert durable_rows(c) == before

    successor = book.create_from_period(report(flow, kind))
    assert successor['snapshot_id'] != snapshot['snapshot_id'] and successor['status'] == 'draft'
    assert service.project_for_plan(CHILD, day)['constraints'] == []
    approved = client.post('/v1/parent/report-snapshots/' + successor['snapshot_id'] + '/approve',
        headers=headers, json={})
    assert approved.status_code == 200, approved.text
    target = approved.json()
    assert service.project_for_plan(CHILD, day)['constraints'] == []
    bound = bind(client, headers, target, day, 'readonly-successor')
    with flow.db.connect() as c:
        before = durable_rows(c)
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        raw = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (target['snapshot_id'],)).fetchone()
        assert book.snapshot_source_current(c, raw)
        assert service.current_feedback(c, raw) == {'feedback_current': None,
            'feedback_current_revision': 0, 'feedback_current_status': 'none'}
        projected = service.project_for_plan(CHILD, day, connection=c)
        assert projected['items'] == []
        assert [item['adjustment_id'] for item in projected['constraints']] == [bound['adjustment_id']]
        assert c.total_changes == 0 and durable_rows(c) == before
    with flow.db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        before, changes = durable_rows(c), c.total_changes
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        projected = service.project_for_plan(CHILD, day, connection=c)
        assert projected['items'] == [] and projected['constraints'] == []
        assert service.project_for_plan(CHILD, day, connection=c) == projected
        assert service.coaching_experiences(c, CHILD) == history
        assert c.total_changes == changes and durable_rows(c) == before
    restarted = ParentReportService(Database(flow.db.path))
    assert restarted.get(target['snapshot_id'])['status'] == 'approved'
    assert restarted.get(target['snapshot_id'])['content'] == target['content']
    assert restarted.get(snapshot['snapshot_id'])['content'] == snapshot['content']


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_period_coaching_drops_retracted_feedback_not_just_plan_projection(flow, kind):
    answer(flow)
    book = ParentReportService(flow.db)
    original = report(flow, kind)
    snapshot = book.create_from_period(original)
    book.approve(snapshot['snapshot_id'])
    book.record_feedback(snapshot['snapshot_id'], 5, 5, 'Synthetic parent feedback', 'feedback')
    assert flow.reports.get(original['period_report_id'])['content']['parent_coaching']['adaptation']['feedback_count'] == 1
    ReportFollowupService(flow.db).retract_feedback(CHILD, snapshot['snapshot_id'], 1, 'retract', 'Misclicked')
    assert flow.reports.get(original['period_report_id'])['content']['parent_coaching']['adaptation']['feedback_count'] == 0

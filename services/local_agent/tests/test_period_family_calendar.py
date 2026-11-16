"""Nanjing report boundaries use evidence instants, never the host calendar."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.family_clock import FAMILY_ZONE
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.repository import coaching_evidence_ref
from home_study_agent import period_written_observations as originals
from test_period_report_reliable_evidence import flow, learning_clock, answer, case_for, transfer, summary, CHILD


@pytest.mark.parametrize('instant,expected', [
    ('2026-09-06T15:59:59.999999+00:00', '2026-09-06'),
    ('2026-09-06T16:00:00+00:00', '2026-09-07'),
    ('2026-09-07T00:00:00+08:00', '2026-09-07'),
    ('2026-09-06T09:00:00-07:00', '2026-09-07'),
    ('2026-09-06T16:00:00Z', '2026-09-07'),
    ('2026-09-06T16:00:00', '2026-09-06'),
])
def test_period_evidence_date_uses_family_zone_and_preserves_legacy_naive_dates(instant, expected):
    assert PeriodReportService._evidence_date(instant).isoformat() == expected


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_midnight_answer_mistake_and_transfer_survive_period_restart(flow, kind):
    f = flow
    day = (f.reference + timedelta(days=7) if kind == 'weekly'
           else (f.reference + timedelta(days=35)).replace(day=1))
    f.clock[0] = datetime.combine(day, datetime.min.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc).isoformat()
    original = answer(f)
    case = case_for(f, original)
    transfer(f, case)
    with f.db.connect() as c:
        journal = [tuple(r) for r in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
    current = f.reports.generate(CHILD, kind, day)
    data = summary(current)
    assert data['coverage']['eligible_evidence_count'] == 2
    assert len(data['new_weaknesses']) == len(data['independent_variant_retests']) == 1
    assert {r['date'] for r in data['evidence'] + data['new_weaknesses'] + data['independent_variant_retests']} == {day.isoformat()}
    previous = summary(f.reports.generate(CHILD, kind, day - timedelta(days=1)))
    assert previous['coverage']['eligible_evidence_count'] == 0
    assert previous['new_weaknesses'] == previous['independent_variant_retests'] == []
    reopened = PeriodReportService(Database(f.db.path))
    assert summary(reopened.get(current['period_report_id'])) == data
    parents = ParentReportService(f.db)
    snapshot = parents.create_from_period(current)
    parents.approve(snapshot['snapshot_id'])
    assert parents.validate_current_for_delivery(snapshot['snapshot_id'])
    ref = data['evidence'][0]['evidence_ref']
    reopened.retract_evidence(CHILD, ref, 'Verify this original', 'midnight-withdraw')
    assert not parents.validate_current_for_delivery(snapshot['snapshot_id'])
    assert reopened.retracted_evidence(CHILD)[0]['date'] == day.isoformat()
    with f.db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
        versions = reopened._evidence_action_versions(c, CHILD, current['period_start'], current['period_end'])
        assert versions
    assert summary(reopened.get(current['period_report_id']))['coverage']['eligible_evidence_count'] == 0


def test_original_observation_selection_and_display_use_the_same_local_day(flow, monkeypatch):
    f = flow
    day = f.reference + timedelta(days=7)
    f.clock[0] = datetime.combine(day, datetime.min.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc).isoformat()
    attempt = answer(f)
    evidence = attempt['evidence_id']
    # Only the score admission boundary is synthetic; selection is actual SQLite.
    monkeypatch.setattr(originals, '_possible_written_originals', lambda *a: {evidence})
    monkeypatch.setattr(originals, 'written_score_candidates', lambda *a: [{'assessment_id': 'local-test'}])
    monkeypatch.setattr(originals, 'current_written_score', lambda *a, **kw: {
        'current': {'source_evidence_id': evidence}, 'receipt': {'independent': True, 'hint_level': 0},
        'decision': {'status': 'reviewed_human', 'passed': False, 'authority_id': 'test-review', 'review_revision': 1}})
    with f.db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS rubric_assessment(assessment_id TEXT)')
        c.commit()
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        changes = c.total_changes
        observations = originals.observations_for_period(c, CHILD, day, day, {}, now=datetime.fromisoformat(f.clock[0]))
        assert len(observations) == 1 and observations[0]['date'] == day.isoformat()
        assert not observations[0]['mastery_claim_allowed']
        previous = day - timedelta(days=1)
        assert originals.observations_for_period(c, CHILD, previous, previous, {}, now=datetime.fromisoformat(f.clock[0])) == []
        assert c.total_changes == changes


def test_future_week_reminders_do_not_displace_current_week_before_limit(flow):
    f = flow
    start = f.reference + timedelta(days=7)
    local = datetime.combine(start, datetime.min.time(), tzinfo=FAMILY_ZONE)
    rows = [('inside', local)] + [(f'outside-{i}', local + timedelta(days=7, minutes=i + 1)) for i in range(6)]
    f.clock[0] = (local + timedelta(days=7, hours=1)).astimezone(timezone.utc).isoformat()
    with f.db.connect() as c:
        for key, instant in rows:
            at = instant.astimezone(timezone.utc).isoformat()
            c.execute('''INSERT INTO intervention_record(intervention_id,child_id,session_id,
                trigger_state,confidence,stage,message_text,policy_version,child_response,status,created_at,responded_at)
                VALUES (?,?,?,'synthetic',0.8,'offer','Synthetic reminder','test','accepted','responded',?,?)''',
                (key, CHILD, f.sid, at, at))
        c.commit()
    result = f.reports.generate(CHILD, 'weekly', start + timedelta(days=6))
    refs = result['content']['parent_coaching']['evidence_refs']
    assert coaching_evidence_ref(CHILD, 'intervention', 'inside') in refs
    assert not any(coaching_evidence_ref(CHILD, 'intervention', key) in refs for key, _ in rows[1:])


def test_calendar_policy_change_requires_fresh_approval_without_rewriting_parent_content(flow, monkeypatch):
    from home_study_agent import family_clock
    f = flow
    answer(f, '90')
    parents = ParentReportService(f.db)
    with monkeypatch.context() as old_policy:
        old_policy.setattr(family_clock, 'EVIDENCE_CALENDAR_VERSION', 'legacy-calendar-policy', raising=False)
        report = f.reports.generate(CHILD, 'weekly', f.reference)
        previous = parents.create_from_period(report)
        parents.edit_draft(previous['snapshot_id'], parent_note='Keep this parent note')
        parents.approve(previous['snapshot_id'])
        with f.db.connect() as c:
            frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (previous['snapshot_id'],)).fetchone())
            assert parents.snapshot_source_current(c, frozen)
    with f.db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        changes = c.total_changes
        assert not parents.snapshot_source_current(c, frozen)
        assert c.total_changes == changes
    with pytest.raises(SnapshotStateError):
        parents.create_from_period(report)
    refreshed = PeriodReportService(Database(f.db.path)).get(report['period_report_id'], persist_refresh=True)
    replacement = parents.create_from_period(refreshed)
    assert replacement['snapshot_id'] != previous['snapshot_id']
    assert replacement['status'] == 'draft'
    assert not parents.validate_current_for_delivery(replacement['snapshot_id'])
    with f.db.connect() as c:
        old = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (previous['snapshot_id'],)).fetchone()
        assert old['status'] == 'revoked'
        assert old['content_json'] == frozen['content_json']
        assert old['parent_edit_json'] == frozen['parent_edit_json']
        assert json.loads(old['parent_edit_json'])['parent_note'] == 'Keep this parent note'
    restarted = ParentReportService(Database(f.db.path))
    assert restarted.get(replacement['snapshot_id'])['status'] == 'draft'
    restarted.approve(replacement['snapshot_id'])
    assert restarted.validate_current_for_delivery(replacement['snapshot_id'])

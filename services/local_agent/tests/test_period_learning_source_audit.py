"""Independent source-only stamp counterexamples; synthetic real SQLite."""
import sqlite3

import pytest

from home_study_agent.goal_program import GoalProgramService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_daily_report_evidence_versions import flow, CHILD


def approved(flow):
    db, _, _, delivery = flow
    periods = PeriodReportService(db)
    report = periods.generate(CHILD, 'weekly')
    parents = delivery.snapshots
    parents.set_period_report_service(periods)
    snapshot = parents.create_from_period(report)
    parents.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    return periods, parents, report, frozen


def test_current_knowledge_label_change_cannot_lend_authority_to_old_period(flow):
    db = flow[0]
    periods, parents, report, frozen = approved(flow)
    with db.connect() as c:
        node = c.execute('SELECT knowledge_node_id FROM learning_evidence WHERE child_id=? LIMIT 1', (CHILD,)).fetchone()[0]
        c.execute('UPDATE knowledge_node SET title=? WHERE knowledge_node_id=?', ('Corrected actual knowledge label', node))
        c.commit()
    fresh = periods.get(report['period_report_id'])
    assert fresh['content']['learning_summary'] != report['content']['learning_summary']
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)


def test_real_parent_goal_configuration_invalidates_old_period_progress(flow):
    db = flow[0]
    goals = GoalProgramService(db)
    goals.cycles(CHILD)
    periods, parents, report, frozen = approved(flow)
    goals.configure_program(CHILD, 'pet_path', target_statement='Confirmed revised PET target',
        target_date=None, target_score=None, target_score_label=None, event_name=None,
        event_rules_url=None, event_version=None, award_target=None, registration_deadline=None,
        weekly_minutes=120, weekly_sessions=6, client_action_id='audit-parent-goal-change')
    fresh = periods.get(report['period_report_id'])
    assert fresh['content']['goal_progress'] != report['content']['goal_progress']
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)


def test_parent_experience_change_does_not_keep_old_coaching_source_current(flow):
    db = flow[0]
    periods, parents, report, frozen = approved(flow)
    parents.record_feedback(frozen['snapshot_id'], 1, 5, 'Too much pressure', 'audit-period-stress')
    fresh = periods.get(report['period_report_id'])
    assert fresh['content']['parent_coaching'] != report['content']['parent_coaching']
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)


def test_repeated_period_refresh_does_not_oscillate_on_its_own_feedback(flow):
    periods, parents, report, frozen = approved(flow)
    parents.record_feedback(frozen['snapshot_id'], 1, 5, 'Too much pressure', 'audit-feedback-loop')
    durations = [periods.get(report['period_report_id'], persist_refresh=True)
                 ['content']['parent_coaching']['action']['duration_minutes'] for _ in range(3)]
    assert len(set(durations)) == 1, durations


def test_source_stamp_borrows_reader_without_writes_or_snapshot_cycles(flow, monkeypatch):
    db, _, _, _ = flow
    periods, parents, report, frozen = approved(flow)
    tables = set()
    qualification_kinds = []
    original = ParentReportService.source_qualification_fingerprint
    def tracked(c, child, kind, source):
        qualification_kinds.append(kind)
        return original(c, child, kind, source)
    monkeypatch.setattr(ParentReportService, 'source_qualification_fingerprint', staticmethod(tracked))
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        def authorize(action, first, second, schema, trigger):
            if action == sqlite3.SQLITE_READ:
                tables.add(first)
            return sqlite3.SQLITE_OK
        c.set_authorizer(authorize)
        before = ParentReportService.period_learning_sources(c, CHILD, report['period_start'], report['period_end'])
        assert c.in_transaction
        assert c.total_changes == 0
        assert c.execute('SELECT 1').fetchone()[0] == 1
        assert before == ParentReportService.period_learning_sources(c, CHILD, report['period_start'], report['period_end'])
        assert 'period_report' not in tables
        assert 'period_report' not in qualification_kinds
        c.set_authorizer(None)

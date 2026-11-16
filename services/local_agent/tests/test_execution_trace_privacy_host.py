"""Trace links follow actual source deletion without copying source contents."""
import json
from uuid import uuid4

import pytest

from home_study_agent.execution_trace import ExecutionTraceService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_daily_report_evidence_versions import flow, CHILD


def traces(flow):
    db, repo, report, _ = flow
    service = ExecutionTraceService(db)
    service.initialize()
    other = 'child-junior-3'
    other_sid = repo.start_session(other, 'other-trace-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (other_sid,))
        for child, sid in ((CHILD, report['session_id']), (other, other_sid)):
            turn, trace = str(uuid4()), str(uuid4())
            c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,tool_status,orchestrator_version,created_at,client_action_id)
                VALUES(?,?,?,'private synthetic text','report','','{}','completed','test','now',?)""", (turn, child, sid, turn))
            sources = [('agent_turn', turn, 'request_received')]
            if child == CHILD:
                attempt = c.execute('SELECT attempt_id FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0]
                sources += [('daily_report', report['report_id'], 'report_written'), ('diagnostic_attempt', attempt, 'learning_result')]
            root = None
            for kind, identifier, event in sources:
                args = dict(child_id=child, session_id=sid, turn_id=turn, step_id=None, source_kind=kind, source_id=identifier)
                version = service.source_version(connection=c, **args)
                span = str(uuid4())
                service.record_link(connection=c, **args, source_version=version, trace_id=trace, span_id=span,
                    parent_span_id=root, event_type=event, elapsed_ms=0, expected_revision=0, client_action_id=str(uuid4()))
                root = root or span
        c.commit()
    return service


@pytest.mark.parametrize('scope,remaining', [('reports', {'agent_turn', 'diagnostic_attempt'}),
    ('learning', {'agent_turn', 'daily_report'}), ('media', set()), ('all_history', set())])
def test_actual_export_and_scope_delete_remove_owned_trace_sources(flow, tmp_path, scope, remaining):
    traces(flow)
    db = flow[0]
    lifecycle = PrivacyLifecycleService(db, tmp_path)
    lifecycle.initialize()
    with open(lifecycle.export_child(CHILD)['path']) as stream:
        exported = json.load(stream)
    assert len(exported['execution_trace_events']) == 3
    assert 'private synthetic text' not in json.dumps(exported['execution_trace_events'])
    assert lifecycle.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))['status'] == 'completed'
    with db.connect() as c:
        assert {row[0] for row in c.execute('SELECT source_kind FROM execution_trace_event WHERE child_id=?', (CHILD,))} == remaining
        assert c.execute('SELECT count(*) FROM execution_trace_event WHERE child_id=?', ('child-junior-3',)).fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_report_retention_removes_trace_reference_but_keeps_other_evidence(flow, tmp_path):
    traces(flow)
    db, _, report, _ = flow
    with db.connect() as c:
        c.execute("UPDATE daily_report SET report_date='2000-01-01' WHERE report_id=?", (report['report_id'],))
        c.commit()
    service = PrivacyLifecycleService(db, tmp_path)
    service.initialize()
    assert service.run_retention()['deleted_counts']['execution_trace_event'] == 1
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM execution_trace_event WHERE source_kind='daily_report'").fetchone()
        assert c.execute('SELECT count(*) FROM execution_trace_event').fetchone()[0] == 3


def test_portable_restore_preserves_append_only_trace_and_actual_source_links(flow, tmp_path):
    from home_study_agent.database import Database
    traces(flow)
    db = flow[0]
    source = PrivacyLifecycleService(db, tmp_path, b's' * 32)
    source.initialize()
    archive = source.create_portable_backup('Synthetic portable backup phrase')
    destination = tmp_path / 'other-device'
    destination.mkdir()
    target_db = Database(destination / 'study.db')
    target_db.initialize()
    target = PrivacyLifecycleService(target_db, destination, b't' * 32)
    target.initialize()
    target.restore_portable_backup(archive['path'], 'Synthetic portable backup phrase', '确认迁移恢复')
    with db.connect() as c, Database(target_db.path).connect() as restored:
        assert [tuple(r) for r in c.execute('SELECT * FROM execution_trace_event ORDER BY event_id')] == [tuple(r) for r in restored.execute('SELECT * FROM execution_trace_event ORDER BY event_id')]
        turn = restored.execute('SELECT turn_id FROM execution_trace_event WHERE child_id=?', (CHILD,)).fetchone()[0]
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()
    view = ExecutionTraceService(Database(target_db.path)).read(CHILD, turn)
    assert len(view['spans']) == 3 and all(row['source_current'] for row in view['spans'])
    assert not view['outcome_evidence_created']

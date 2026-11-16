"""Only synthetic records; trace metadata never supplies outcome evidence."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.execution_trace import ExecutionTraceService, TraceConflict, TraceInputError
from test_daily_report_evidence_versions import flow as learning_flow, CHILD


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / 'trace.db')
    db.initialize()
    with db.connect() as c:
        for child in ('one', 'two'):
            c.execute('INSERT INTO child_profile VALUES (?,?,?,?,?,?,?)', (child,'SYNTHETIC_PRIVATE','4','local','active','now','now'))
            c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                VALUES (?,?,'completed','reflection','now','now',?)''', (child+'-session',child,child+'-session-key'))
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
                orchestrator_version,tool_status,created_at,client_action_id)
                VALUES (?,?,?,'SYNTHETIC_PRIVATE','private_intent','SYNTHETIC_PRIVATE','{}','v1','pending','now',?)''',
                (child+'-turn',child,child+'-session',child+'-turn-key'))
            c.execute('''INSERT INTO agent_plan VALUES (?,NULL,?,?,?,'v1','{}','{}','running','now','now')''',
                (child+'-plan',child,child+'-session',child+'-turn'))
            c.execute('''INSERT INTO agent_step VALUES (?,?,0,'diagnostic.submit','v1','server','low','{}','{}',
                'unknown',?,'{"private":"SYNTHETIC_PRIVATE"}','MODEL_TIMEOUT','now','now')''',
                (child+'-step',child+'-plan',child+'-step-key'))
        c.commit()
    service = ExecutionTraceService(db)
    service.initialize()
    return db, service


def link(service, c, **overrides):
    args = dict(child_id='one',session_id='one-session',turn_id='one-turn',step_id='one-step',
        trace_id=str(uuid4()),span_id=str(uuid4()),parent_span_id=None,event_type='action_result',
        source_kind='agent_step',source_id='one-step',elapsed_ms=125,expected_revision=0,
        client_action_id='synthetic-key',operation='observe')
    args.update(overrides)
    args.setdefault('source_version', service.source_version(connection=c, **{k:args[k] for k in
        ('child_id','session_id','turn_id','step_id','source_kind','source_id')}))
    return service.record_link(connection=c, **args), args


def test_real_fk_projection_is_local_only_and_has_no_raw_payload_or_success_claim(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        event, args = link(service,c)
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0
        c.commit()
    result = service.read('one','one-turn')
    assert result['spans'][0]['source_status'] == 'unknown'
    assert result['spans'][0]['error_code'] == 'MODEL_TIMEOUT'
    assert result['spans'][0]['elapsed_ms'] == 125
    assert result['spans'][0]['recovery_action'] == 'check_receipt_before_retry'
    assert result['spans'][0]['source_current'] is True
    assert result['missing_stages'] and not result['outcome_evidence_created']
    assert result['local_only'] and not result['cloud_transmission_allowed']
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
    assert ExecutionTraceService(Database(db.path)).read('one','one-turn') == result


def test_borrowed_transaction_rollback_and_exact_replay(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        event, args = link(service,c)
        assert service.record_link(connection=c, **args) == event
        with pytest.raises(TraceConflict):
            service.record_link(connection=c, **{**args,'elapsed_ms':126})
        c.rollback()
    assert service.read('one','one-turn')['spans'] == []
    with db.connect() as c:
        with pytest.raises(TraceInputError, match='TRANSACTION'):
            service.record_link(connection=c, **args)


def test_versions_and_retraction_preserve_history_without_rewriting_source(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        event, args = link(service,c)
        c.commit()
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='failed',error_code='APP_RESTARTED' WHERE step_id='one-step'")
        c.commit()
    assert not service.read('one','one-turn')['spans'][0]['source_current']
    assert 'action_result' not in service.read('one','one-turn')['missing_stages']
    assert 'action_result' in service.read('one','one-turn')['changed_stages']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(TraceConflict, match='SOURCE'):
            service.record_link(connection=c, **{**args,'client_action_id':'stale','expected_revision':1})
        version = service.source_version(connection=c, **{k:args[k] for k in
            ('child_id','session_id','turn_id','step_id','source_kind','source_id')})
        latest = service.record_link(connection=c, **{**args,'source_version':version,'expected_revision':1,'client_action_id':'revision'})
        service.record_link(connection=c, **{**args,'source_version':version,'expected_revision':2,'operation':'retract','client_action_id':'retract'})
        c.commit()
    result = service.read('one','one-turn')
    assert result['spans'] == []
    assert 'action_result' not in result['missing_stages']
    assert 'action_result' in result['changed_stages']
    assert [row['revision'] for row in result['history']] == [1,2,3]
    assert latest['revision'] == 2
    with db.connect() as c:
        assert c.execute("SELECT status FROM agent_step WHERE step_id='one-step'").fetchone()[0] == 'failed'


def test_cross_child_wrong_step_trace_reparenting_and_free_text_are_rejected(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        event, args = link(service,c)
        for changes in ({'source_id':'two-step'},{'step_id':'two-step'},{'session_id':'two-session'},
                        {'trace_id':'SYNTHETIC_PRIVATE'}, {'elapsed_ms':True}, {'event_type':'SYNTHETIC_PRIVATE'}):
            with pytest.raises((TraceInputError,TraceConflict,KeyError)):
                service.record_link(connection=c, **{**args, **changes, 'client_action_id':str(uuid4())})
        with pytest.raises(TraceConflict):
            service.record_link(connection=c, **{**args,'span_id':str(uuid4()),'parent_span_id':str(uuid4()),'client_action_id':'parent'})
        c.commit()
    with pytest.raises(KeyError):
        service.read('two','one-turn')


def test_executor_persisted_error_class_remains_diagnosable_without_exception_message(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE agent_step SET status='failed',error_code='AgentExecutionConflict' WHERE step_id='one-step'")
        event, _ = link(service,c)
        assert event['error_code'] == 'AgentExecutionConflict'
        assert event['recovery_action'] == 'check_receipt_before_retry'
        c.commit()


def test_reads_do_not_initialize_schema_or_write_and_privacy_hard_delete_is_allowed(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        link(service,c)
        c.commit()
        before = '\n'.join(c.iterdump())
        c.execute('PRAGMA query_only=ON')
        service.read('one','one-turn',connection=c)
        assert '\n'.join(c.iterdump()) == before
    with db.connect() as c:
        c.execute("DELETE FROM agent_turn WHERE agent_turn_id='one-turn'")
        assert c.execute('SELECT COUNT(*) FROM execution_trace_event').fetchone()[0] == 0
        c.commit()


def test_missing_schema_is_readable_but_business_write_never_initializes_it(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('DROP TABLE execution_trace_event')
        c.commit()
        assert service.read('one','one-turn',connection=c)['spans'] == []
        with pytest.raises(TraceInputError, match='SCHEMA_NOT_INITIALIZED'):
            link(service,c)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='execution_trace_event'").fetchone()


def test_concurrent_exact_key_and_cas_do_not_duplicate_events(flow):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        _, args = link(service,c)
        c.rollback()
    def submit(delta):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                result = service.record_link(connection=c, **{**args, **delta})
                c.commit()
                return result['event_id']
            except TraceConflict:
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        exact = list(executor.map(submit,[{},{}]))
    assert exact[0] == exact[1] != 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        revised = list(executor.map(submit,[{'expected_revision':1,'client_action_id':'a'},
                                           {'expected_revision':1,'client_action_id':'b'}]))
    assert revised.count('conflict') == 1
    assert len(service.read('one','one-turn')['history']) == 2


def test_actual_scoring_and_report_links_are_readonly_not_new_mastery_facts(learning_flow):
    db, repo, report, _ = learning_flow
    sid = report['session_id']
    service = ExecutionTraceService(db)
    service.initialize()
    trace, root = str(uuid4()), str(uuid4())
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,tool_status,created_at,client_action_id)
            VALUES ('report-turn',?,?,'SYNTHETIC_PRIVATE','report','SYNTHETIC_PRIVATE','{}','v1','completed','now','report-turn')''', (CHILD,sid))
        base = dict(child_id=CHILD,session_id=sid,turn_id='report-turn',step_id=None,trace_id=trace,
            elapsed_ms=1,expected_revision=0)
        def record(span,parent,kind,identifier,event):
            args = {**base,'span_id':span,'parent_span_id':parent,'source_kind':kind,'source_id':identifier,'event_type':event,'client_action_id':span}
            args['source_version'] = service.source_version(connection=c,**{k:args[k] for k in
                ('child_id','session_id','turn_id','step_id','source_kind','source_id')})
            return service.record_link(connection=c,**args), args
        record(root,None,'agent_turn','report-turn','request_received')
        attempt = c.execute('SELECT attempt_id FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]
        record(str(uuid4()),root,'diagnostic_attempt',attempt,'learning_result')
        _, saved = record(str(uuid4()),root,'daily_report',report['report_id'],'report_written')
        before = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence')]
        assert len(service.read(CHILD,'report-turn',connection=c)['spans']) == 3
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence')] == before
        c.commit()
    with db.connect() as c:
        c.execute('DELETE FROM daily_report WHERE report_id=?',(report['report_id'],))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        result = service.read(CHILD,'report-turn',connection=c)
        assert not next(s for s in result['spans'] if s['source_kind']=='daily_report')['source_current']
        service.record_link(connection=c,**{**saved,'expected_revision':1,'operation':'retract','client_action_id':'remove-stale-link'})
        c.commit()
    state = service.read(CHILD,'report-turn')
    assert 'report_written' not in state['missing_stages']
    assert 'report_written' in state['changed_stages']


@pytest.mark.parametrize('column,value', [
    ('result_json', '{"status":"actually_changed"}'), ('status', 'cancelled'),
    ('error_code', 'DEVICE_COMMAND_FAILED'), ('arguments_json', '{"different":true}'),
    ('capability_version', 'v2'), ('capability_name', 'task.pause'),
])
def test_step_timestamp_is_not_a_revision_but_business_fields_are(flow,column,value):
    db, service = flow
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        link(service,c)
        c.commit()
        c.execute("UPDATE agent_step SET updated_at='later' WHERE step_id='one-step'")
        c.commit()
    assert service.read('one','one-turn')['spans'][0]['source_current'] is True
    with db.connect() as c:
        c.execute(f"UPDATE agent_step SET {column}=? WHERE step_id='one-step'", (value,))
        c.commit()
    state = service.read('one','one-turn')
    assert state['spans'][0]['source_current'] is False
    assert 'action_result' not in state['missing_stages']
    assert state['changed_stages'] == ['action_result']


def test_legacy_hashes_remain_readable_but_are_not_reinterpreted_or_backfilled(flow,monkeypatch):
    from home_study_agent import execution_trace as trace_module
    db, service = flow
    version = trace_module._source_version
    # Produce a genuine v1 event through the original hash policy, not by
    # overwriting append-only history or treating a new hash as an old one.
    with monkeypatch.context() as patch:
        patch.setattr(trace_module, '_source_version', lambda kind,row: version(kind,row,legacy=True))
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            event, _ = link(service,c)
            c.commit()
    assert event['source_version'].startswith('trace_source_v1:')
    assert service.read('one','one-turn')['spans'][0]['source_current'] is True
    with db.connect() as c:
        c.execute("UPDATE agent_step SET updated_at='later' WHERE step_id='one-step'")
        c.commit()
        c.execute('PRAGMA query_only=ON')
        state = ExecutionTraceService(Database(db.path)).read('one','one-turn',connection=c)
        assert state['spans'][0]['source_current'] is False
        assert state['changed_stages'] == ['action_result']
        assert 'action_result' not in state['missing_stages']
        assert state['history'] == [event]
        assert c.execute('SELECT COUNT(*) FROM execution_trace_event').fetchone()[0] == 1

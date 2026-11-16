"""Actual prompt/runtime receipts must not retain an erased source snapshot."""
import json
import sqlite3
from uuid import uuid4

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, action_from_plan, parse_agent_plan_output
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
from home_study_agent.repository import StudyRepository, utc_now
from test_agent_runtime import learning_plan_json
from test_native_source_privacy_cleanup import native, cleanup, CHILD, PROMPT

OTHER = 'child-junior-3'


def prompt_command(db, child, sid, item, *, channel='result'):
    prompts = DiagnosticPromptService(db)
    prepared = prompts.read(sid)
    if prepared['status'] != 'waiting_answer':
        prepared = prompts.prepare(sid, preferred_item_id=item, client_action_id=str(uuid4()))
    prompt = prepared['prompt']
    assert prompt, prepared
    runtime = AgentRuntimeStore(db)
    payload = json.loads(learning_plan_json())
    payload['requires_child_reply'] = False
    payload['next_step'].update(capability='diagnostic.start', arguments={}, expected_evidence=['diagnostic_item_presented'])
    if channel == 'argument':
        payload['next_step'].update(capability='diagnostic.replay', arguments={'diagnostic_prompt_id': prompt['prompt_id']})
    decision = parse_agent_plan_output(json.dumps(payload), 'study', CapabilityRegistry())
    turn = str(uuid4())
    with db.connect() as c:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (turn,child,sid,'Original child request','diagnostic.start','',json.dumps(action_from_plan(decision, 'Original child request', None)),
             'isolated-native-runtime',utc_now(),turn))
        runtime.persist_plan(c, child_id=child, session_id=sid, agent_turn_id=turn, client_action_id=turn,
            decision=decision, context_manifest={'goal_prompt_binding': {'prompt_id': prompt['prompt_id']}} if channel == 'manifest' else {})
        if channel == 'result':
            runtime.complete_turn_execution(c, turn, True, None, {'client_bridge_receipt': {
                'capability': 'diagnostic.start', 'execution_location': 'client_device_bridge',
                'action_type': 'start_diagnostic', 'status': 'completed', 'completed_at': utc_now(), 'output': prepared}})
        else:
            # An actual failed bridge attempt can still have frozen source
            # bindings, but no successful output or new learning evidence.
            runtime.complete_turn_execution(c, turn, False, 'DEVICE_UNAVAILABLE')
        c.commit()
    return turn, prompt


def snapshot(c, turn):
    return dict(c.execute('''SELECT s.*,p.plan_json,p.context_manifest_json,p.status AS plan_status,
        t.action_json,t.transcript,t.response_text FROM agent_step s JOIN agent_plan p USING(plan_id)
        JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id WHERE t.agent_turn_id=?''', (turn,)).fetchone())


@pytest.mark.parametrize('channel', ['result', 'argument', 'manifest'])
def test_exact_prompt_runtime_source_erased_and_other_child_unchanged(native, channel):
    db, _, item, _, sid = native
    turn, prompt = prompt_command(db, CHILD, sid, item['diagnostic_item_id'], channel=channel)
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)
    other_sid = StudyRepository(db).switch_active_session_child(sid, OTHER, 'other-runtime')['session_id']
    other_turn, _ = prompt_command(db, OTHER, other_sid, 'diag-j3-discriminant-1')
    with db.connect() as c:
        other_before = snapshot(c, other_turn)
        before = snapshot(c, turn)
        if channel == 'result':
            assert PROMPT in before['result_json'] and item['diagnostic_item_id'] not in before['arguments_json']
        c.execute('BEGIN IMMEDIATE')
        counts = cleanup(c, {})
        after = snapshot(c, turn)
        assert after['error_code'] == 'SOURCE_DELETED'
        assert after['status'] in {'completed', 'cancelled'}
        assert after['result_json'] is None and after['plan_json'] == '{}'
        assert after['context_manifest_json'] == '{}' and after['action_json'] == '{}'
        assert after['transcript'] == 'Original child request'
        assert snapshot(c, other_turn) == other_before
        assert counts['agent_step_redacted'] == counts['agent_plan_redacted'] == counts['agent_turn_redacted'] == 1
        assert counts['diagnostic_prompt_redacted'] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    with Database(db.path).connect() as c:
        assert snapshot(c, turn) == after
        assert snapshot(c, other_turn) == other_before
        assert c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[:] == ('invalidated','{}')


def test_two_runtime_receipts_counts_add_and_midway_fault_rolls_back(native):
    db, _, item, _, sid = native
    first, prompt = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
    second, _ = prompt_command(db, CHILD, sid, item['diagnostic_item_id'], channel='argument')
    with db.connect() as c:
        original = [snapshot(c, turn) for turn in (first, second)]
        c.execute("CREATE TRIGGER synthetic_runtime_delete_fault BEFORE UPDATE OF action_json ON agent_turn BEGIN SELECT RAISE(ABORT,'synthetic erasure fault'); END")
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_STORAGE_FAILURE'):
            cleanup(c, scopes)
        assert scopes == {}
        assert [snapshot(c, turn) for turn in (first, second)] == original
        assert c.execute('SELECT item_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] != '{}'
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (item['diagnostic_item_id'],)).fetchone()[0] == PROMPT
        c.rollback()
        c.execute('DROP TRIGGER synthetic_runtime_delete_fault')
        c.commit()
    with Database(db.path).connect() as c:
        c.execute('BEGIN IMMEDIATE')
        counts = cleanup(c, {})
        assert counts['agent_step_redacted'] == counts['agent_plan_redacted'] == counts['agent_turn_redacted'] == 2
        assert counts['diagnostic_prompt_redacted'] == 1
        c.commit()


def test_corrupt_runtime_result_blocks_before_erasure_and_can_retry(native):
    db, _, item, _, sid = native
    turn, _ = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
    with db.connect() as c:
        original = snapshot(c, turn)
        c.execute("UPDATE agent_step SET result_json='{broken' WHERE step_id=?", (original['step_id'],))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_JSON_INVALID'):
            cleanup(c, {})
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (item['diagnostic_item_id'],)).fetchone()[0] == PROMPT
        c.rollback()
        c.execute('UPDATE agent_step SET result_json=? WHERE step_id=?', (original['result_json'], original['step_id']))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        assert cleanup(c, {})['agent_step_redacted'] == 1
        c.commit()


def test_legacy_turn_action_without_plan_is_erased_but_original_utterance_remains(native):
    db, _, item, _, sid = native
    turn, _ = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
    with db.connect() as c:
        row = snapshot(c, turn)
        # Legacy persisted turn without Runtime plan; no fabricated scoring.
        c.execute('DELETE FROM agent_step WHERE step_id=?', (row['step_id'],))
        c.execute('DELETE FROM agent_plan WHERE plan_id=?', (row['plan_id'],))
        c.execute('UPDATE agent_turn SET action_json=?,response_text=? WHERE agent_turn_id=?',
                  (json.dumps({'diagnostic_item_id': item['diagnostic_item_id'], 'question': PROMPT}), PROMPT, turn))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        counts = cleanup(c, {})
        saved = c.execute('SELECT transcript,response_text,action_json,tool_error_code FROM agent_turn WHERE agent_turn_id=?', (turn,)).fetchone()
        assert saved['transcript'] == 'Original child request'
        assert PROMPT not in saved['response_text'] and saved['action_json'] == '{}'
        assert saved['tool_error_code'] == 'SOURCE_DELETED'
        assert counts['agent_turn_redacted'] == 1 and counts.get('agent_step_redacted', 0) == 0
        c.commit()


def test_foreign_prompt_pointer_is_not_authority_to_erase_other_child(native):
    db, _, item, _, sid = native
    _, prompt = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)
    other_sid = StudyRepository(db).switch_active_session_child(sid, OTHER, 'foreign-other')['session_id']
    other_turn, _ = prompt_command(db, OTHER, other_sid, 'diag-j3-discriminant-1')
    with db.connect() as c:
        # Corrupt legacy child/session association must fail closed, not delete
        # the other child's otherwise valid execution snapshot.
        row = snapshot(c, other_turn)
        c.execute('UPDATE agent_step SET arguments_json=? WHERE step_id=?',
                  (json.dumps({'diagnostic_prompt_id': prompt['prompt_id']}), row['step_id']))
        c.commit()
        before = snapshot(c, other_turn)
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_OWNER_MISMATCH'):
            cleanup(c, scopes)
        assert scopes == {} and snapshot(c, other_turn) == before
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (item['diagnostic_item_id'],)).fetchone()[0] == PROMPT
        c.rollback()


@pytest.mark.parametrize('inject_fault', [False, True])
def test_actual_http_runtime_copies_delete_restart_and_fault_retry(native, inject_fault):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, _, item, _, sid = native
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='native-runtime', data_encryption_key=b'0'*32)
    local = {'X-HSC-Session': settings.session_token}
    deletion_id = None
    for restarted in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            token = client.post('/v1/parent/' + ('unlock' if restarted else 'setup'), headers=local, json={'pin':'2468'}).json()['parent_token']
            if not restarted:
                turn, prompt = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
                with db.connect() as c:
                    original = snapshot(c, turn)
                    if inject_fault:
                        c.execute("CREATE TRIGGER synthetic_http_runtime_fault BEFORE UPDATE OF result_json ON agent_step BEGIN SELECT RAISE(ABORT,'synthetic host fault'); END")
                        c.commit()
            response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
                json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-runtime-delete'})
            assert response.headers.get('content-type', '').startswith('application/json'), response.text
            if inject_fault and not restarted:
                assert response.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
                assert response.json()['retryable'] is True
                with db.connect() as c:
                    result = dict(c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='native-runtime-delete'").fetchone())
            else:
                assert response.status_code == 200, response.text
                result = response.json()
            if deletion_id:
                assert result['deletion_id'] == deletion_id
            deletion_id = result['deletion_id']
            with db.connect() as c:
                if inject_fault and not restarted:
                    assert result['status'] == 'failed', result
                    assert snapshot(c, turn) == original
                    assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (item['diagnostic_item_id'],)).fetchone()[0] == PROMPT
                    c.execute('DROP TRIGGER synthetic_http_runtime_fault')
                    c.commit()
                else:
                    assert result['status'] == 'completed', result
                    assert snapshot(c, turn)['result_json'] is None
                    assert snapshot(c, turn)['transcript'] == 'Original child request'
                    assert c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[:] == ('invalidated','{}')


def test_actual_http_late_failed_receipt_cannot_restore_erased_question(native):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, _, item, _, sid = native
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='native-late', data_encryption_key=b'0'*32)
    local = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        turn, _ = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'late-runtime-delete'})
        assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
        with db.connect() as c:
            erased = snapshot(c, turn)
        late = client.put(f'/v1/agent-turns/{turn}/outcome', headers=local, json={
            'final_response': PROMPT, 'tool_status':'failed', 'tool_error_code':'NETWORK_ERROR',
            'result': {'cached_question': PROMPT}})
        assert late.status_code == 409, late.text
        with db.connect() as c:
            assert snapshot(c, turn) == erased

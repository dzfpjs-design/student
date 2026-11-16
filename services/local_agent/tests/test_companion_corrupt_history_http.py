"""Optional C05 history must not block current local control or parent reads."""
import base64
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_companion_verified_return import returned, execution, goal_db, exact_goal_item, CHILD, OTHER, saved_answer
from home_study_agent.local_media_ai import LocalMediaAI
from test_pet_media_evidence import wav_file


def control(client, settings, monkeypatch, sid, key):
    quote = '暂停高拍仪'
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (quote, 1000, 'fixture', False))
    audio = settings.data_dir / 'synthetic-c05.wav'
    wav_file(audio)
    captured = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64':base64.b64encode(audio.read_bytes()).decode(), 'purpose':'agent_command',
        'client_action_id':key+'-voice', 'capture_reference':{'child_id':CHILD}})
    assert captured.status_code == 200, captured.text
    voice = captured.json()['voice_input_id']
    result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': quote, 'client_action_id': key, 'voice_input_id': voice,
        'cloud_text_allowed': False})
    assert result.status_code == 200, result.text
    assert result.json()['action']['type'] == 'control_document_monitor', result.text
    assert result.json()['action']['command'] == 'pause'
    turn_id = result.json()['agent_turn_id']
    admission = client.post(f'/v1/sessions/{sid}/agent-turns/{turn_id}/control-admission')
    assert admission.status_code == 200, admission.text
    assert admission.json()['status'] == 'admitted', admission.text
    paused = client.post(f'/v1/sessions/{sid}/document-monitor/pause', json={'client_action_id':key+'-pause'})
    assert paused.status_code == 200, paused.text
    receipt = dict(capability='document.monitor_control', execution_location='client_device_bridge',
        action_type='control_document_monitor', status='completed',
        completed_at=datetime.now(timezone.utc).isoformat(),
        # Controlled client bridge: no live camera lease/frame in this fixture.
        # Runtime must still verify the persisted pause and absence of a lease.
        output={'device_type':'document_camera', 'device_status':'paused',
                'frame_confirmed':False, 'lease_confirmed':True, 'persisted_status':'paused'})
    completed = client.put(f'/v1/agent-turns/{turn_id}/outcome', json={
        'final_response':'本地监控暂停记录已保存；没有实机释放证据。', 'tool_status':'completed',
        'result':{'execution_receipt':receipt, 'client_bridge_receipt':receipt}})
    assert completed.status_code == 200, completed.text
    return result.json()


@pytest.mark.parametrize('fault', ['snapshot_json', 'snapshot_array', 'snapshot_null',
                                 'manifest_json', 'manifest_array', 'event_json', 'event_array'])
def test_bad_history_real_control_parent_and_restart(returned, monkeypatch, fault):
    db, sid, _, prompt, answer, good_id, _ = returned
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(args)
        pytest.fail('Local C05 recovery must not call a model')
    monkeypatch.setattr(ModelGateway, 'complete', forbidden)
    settings = Settings(data_dir=db.path.parent, database_path=db.path,
        data_encryption_key=b'c'*32, maintenance_enabled=False)
    route = f'/v1/parent/children/{CHILD}/longitudinal-model'
    outcomes = f'/v1/parent/children/{CHILD}/companion-activity-outcomes'
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin':'7391'}).json()['parent_token']
        headers = {'X-HSC-Parent':token}
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version'],
            'answer':answer, 'client_action_id':'real-return-answer'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'answered'
        assert client.get(route, headers=headers).json()['payload']['companion_balance']['verified_child_return_sessions'] == 1
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start', json={
            'client_action_id':'start-monitor'}).status_code == 200
        prior = control(client, settings, monkeypatch, sid, 'valid-optional-control')
        runtime = AgentRuntimeStore(db)
        runtime.start_companion_activity(sid, 'movement', 'Synthetic unrelated stretch', 'second-activity', None)
        runtime.end_companion_activity(sid, 'turn_limit')
        rows = CompanionActivityOutcomeService(db).list(CHILD)['items']
        bad_id = next(row['outcome_id'] for row in rows if row['outcome_id'] != good_id)
        value = '{broken' if fault.endswith('json') else ('null' if fault.endswith('null') else '[]')
        with db.connect() as c:
            if fault.startswith('snapshot'):
                c.execute('UPDATE companion_activity_outcome SET source_snapshot_json=? WHERE outcome_id=?', (value,bad_id))
            elif fault.startswith('event'):
                # Isolated damaged-backup simulation. Restore the append-only
                # trigger before exercising any product API.
                trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='companion_activity_outcome_event_no_update'").fetchone()[0]
                c.execute('DROP TRIGGER companion_activity_outcome_event_no_update')
                c.execute('UPDATE companion_activity_outcome_event SET payload_json=? WHERE outcome_id=?', (value,bad_id))
                c.execute(trigger)
            else:
                c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',
                          (value,prior['agent_turn_id']))
            c.commit()
        control(client, settings, monkeypatch, sid, 'damaged-history-control')
        check_parent(client, headers, route, outcomes, fault, bad_id)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/unlock', json={'pin':'7391'}).json()['parent_token']
        control(client, settings, monkeypatch, sid, 'restart-history-control')
        check_parent(client, {'X-HSC-Parent':token}, route, outcomes, fault, bad_id)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE client_action_id IN ('damaged-history-control','restart-history-control') AND tool_status='completed'").fetchone()[0] == 2
            if fault.startswith('snapshot'):
                assert c.execute('SELECT source_snapshot_json FROM companion_activity_outcome WHERE outcome_id=?',
                                 (bad_id,)).fetchone()[0] == value
            elif fault.startswith('event'):
                assert c.execute('SELECT payload_json FROM companion_activity_outcome_event WHERE outcome_id=?',
                                 (bad_id,)).fetchone()[0] == value
                assert c.execute("SELECT 1 FROM sqlite_master WHERE name='companion_activity_outcome_event_no_update'").fetchone()
            else:
                assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                 (prior['agent_turn_id'],)).fetchone()[0] == value
    assert calls == []


def check_parent(client, headers, route, outcomes, fault, bad_id):
    response = client.get(route, headers=headers)
    assert response.status_code == 200, response.text
    balance = response.json()['payload']['companion_balance']
    assert balance['verified_child_return_sessions'] == 1
    assert balance['task_resume_receipt_sessions'] == 1
    assert balance['habit_success_sessions'] == 0
    detail = client.get(outcomes+'/'+bad_id, headers=headers)
    assert detail.status_code == 200, detail.text
    if fault.startswith(('snapshot', 'event')):
        assert detail.json()['activity']['source_valid'] is False
        assert detail.json()['activity']['return_verification']['status'] == 'unknown'
        assert detail.json()['activity']['return_verification']['reason'] == 'activity_history_unavailable'
        assert any(e['outcome_id'] == bad_id and e['return_verification']['status'] == 'unknown'
                   for e in balance['outcome_evidence'])
    listed = client.get(outcomes, headers=headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()['items']) == 2
    other = client.get(route.replace(CHILD, OTHER), headers=headers)
    assert other.status_code == 200, other.text
    assert other.json()['payload']['companion_balance']['verified_child_return_sessions'] == 0
    assert client.get(outcomes.replace(CHILD, OTHER)+'/'+bad_id, headers=headers).status_code == 404


def test_optional_c05_reads_do_not_hide_sqlite_fault(returned, monkeypatch):
    from home_study_agent.companion_activity_outcome import project_companion_activity_evidence
    class Broken:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('synthetic read failure')
    with pytest.raises(sqlite3.OperationalError, match='synthetic read failure'):
        project_companion_activity_evidence(Broken(), CHILD, '2026-01-01T00:00:00+00:00')
    with pytest.raises(sqlite3.OperationalError, match='synthetic read failure'):
        CompanionActivityOutcomeService._task_prompts(Broken(), {'child_id':CHILD,'session_id':returned[1]}, {})


@pytest.mark.parametrize('value', ['{broken', '[]', 'null', '{}'])
def test_damage_to_original_snapshot_removes_only_its_qualification(returned, value):
    db, _, _, _, _, oid, _ = returned
    saved_answer(returned)
    service = CompanionActivityOutcomeService(db)
    assert service.get(CHILD, oid)['activity']['return_verification']['status'] == 'verified'
    with db.connect() as c:
        c.execute('UPDATE companion_activity_outcome SET source_snapshot_json=? WHERE outcome_id=?', (value,oid))
        c.commit()
    detail = service.get(CHILD, oid)
    assert detail['activity']['return_verification']['status'] == 'unknown'
    assert detail['activity']['resume']['actual_child_return_verified'] is False
    assert len(detail['history']) == 3
    with db.connect() as c:
        assert c.execute('SELECT source_snapshot_json FROM companion_activity_outcome WHERE outcome_id=?', (oid,)).fetchone()[0] == value
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


@pytest.mark.parametrize('value', ['{broken', '[]', '{"goal_prompt_binding":[]}'])
def test_damaged_original_task_manifest_cannot_donate_return_proof(returned, value):
    db, _, _, prompt, _, oid, _ = returned
    saved_answer(returned)
    with db.connect() as c:
        ids = [r[0] for r in c.execute("SELECT plan_id FROM agent_plan WHERE json_extract(context_manifest_json, '$.goal_prompt_binding.prompt_id')=?", (prompt['prompt_id'],))]
        assert len(ids) == 1
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?', (value,ids[0]))
        c.commit()
    detail = CompanionActivityOutcomeService(db).get(CHILD, oid)['activity']
    assert detail['return_verification']['status'] == 'unknown'
    assert detail['return_verification']['reason'] == 'exact_task_question_binding_unavailable'


@pytest.mark.parametrize('site', ['snapshot_query', 'detail_source', 'manifest_query'])
def test_scoped_optional_reads_propagate_real_database_errors(returned, site):
    from home_study_agent.companion_activity_outcome import project_companion_activity_evidence
    db, sid, _, _, _, oid, _ = returned
    saved_answer(returned)
    with db.connect() as c:
        class ReadFault:
            def execute(self, sql, *args):
                match = {'snapshot_query':'COALESCE(julianday(CASE',
                         'detail_source':'WHERE activity_id=? AND child_id=?',
                         'manifest_query':'SELECT p.context_manifest_json FROM agent_plan'}[site]
                if match in sql:
                    raise sqlite3.OperationalError('synthetic scoped disk failure')
                return c.execute(sql, *args)
        with pytest.raises(sqlite3.OperationalError, match='synthetic scoped disk failure'):
            project_companion_activity_evidence(ReadFault(), CHILD, '2026-01-01T00:00:00+00:00')


def test_unreadable_retraction_never_reactivates_earlier_return(returned):
    db, _, _, _, _, oid, _ = returned
    saved_answer(returned)
    service = CompanionActivityOutcomeService(db)
    retracted = service.retract(CHILD, oid, expected_revision=3, client_action_id='real-retraction',
        target_revision=3, reason='withdraw the resume evidence', actor='parent')
    assert retracted['activity']['return_verification']['status'] == 'unknown'
    with db.connect() as c:
        trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='companion_activity_outcome_event_no_update'").fetchone()[0]
        c.execute('DROP TRIGGER companion_activity_outcome_event_no_update')
        c.execute("UPDATE companion_activity_outcome_event SET payload_json='{}' WHERE event_id=?", (retracted['event']['event_id'],))
        c.execute(trigger)
        c.commit()
    activity = service.get(CHILD, oid)['activity']
    assert activity['return_verification']['status'] == 'unknown'
    assert activity['return_verification']['reason'] == 'activity_history_unavailable'
    assert activity['resume']['actual_child_return_verified'] is False

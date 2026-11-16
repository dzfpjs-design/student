"""Public synthetic original material -> recording -> atomic durable blocked intent.

ASR is a controlled transcript boundary. No acoustic model or score is executed.
"""
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, AgentPlanDecision, ObjectiveDraft, PlannedStep
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD, OTHER
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_voice_runtime_binding import capture, interpret, submit


@pytest.fixture
def original_read_aloud(tmp_path, monkeypatch):
    db, settings = setup(tmp_path)
    gateway = Gateway({}); gateway.configured = False
    from home_study_agent import pet_speaking_candidate_jobs as jobs
    from home_study_agent import pet_multipa_offline as offline
    from home_study_agent import pet_multipa_epoch as epoch
    assert epoch._REVIEWED_DEPLOYMENTS == {}
    monkeypatch.setattr(offline, 'predict_in_worker', lambda *a, **k: pytest.fail('REFERENCE_WORKER_MUST_NOT_RUN'))
    registered = []
    original = jobs.register_new_feedback_intent
    def register(c, *args):
        assert c.in_transaction
        result = original(c, *args)
        if result:
            registered.append(result)
            assert c.execute("SELECT count(*) FROM learning_evaluation_execution WHERE state='completed'").fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM pet_speaking_candidate_job').fetchone()[0] == 1
        return result
    monkeypatch.setattr(jobs, 'register_new_feedback_intent', register)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41910)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True}).status_code == 200
        source_text, source, draft = authored(client, db, 'read_aloud')
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review':
                body.update(decision='approve', note='Public synthetic original passage review, no learner score')
            published = post(client, BASE + '/' + draft['candidate_id'] + '/' + operation, body)
        assert published['eligible']
        item_id = published['diagnostic_item_id']
        sid = start(client)
        runtime = AgentRuntimeStore(db); turn = uuid4().hex; stamp = datetime.now(timezone.utc).isoformat()
        with db.connect() as c:
            c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','Public synthetic original reading goal',?,'diagnose','active',?,?,NULL,NULL)",
                (uuid4().hex, CHILD, sid, json.dumps(['Read the reviewed original passage']), stamp, stamp))
            c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)VALUES(?,?,?,'','start_diagnostic','','{\"type\":\"start_diagnostic\"}','pending','test',?,?)", (turn, CHILD, sid, stamp, turn))
            cap = CapabilityRegistry().require('diagnostic.start', 'study')
            runtime.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=turn, client_action_id=turn, context_manifest={},
                decision=AgentPlanDecision(ObjectiveDraft('pause', None, None, (), 'execution'), PlannedStep(cap, {'track': 'pet'}, 'Original read aloud', cap.expected_evidence), '', True, 1))
            c.commit()
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={'track': 'pet', 'preferred_item_id': item_id, 'client_action_id': 'reference-question'})
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        assert prompt['item']['stimulus_text'] == source_text['stimulus_text']
        assert prompt['item']['prompt'] != source_text['stimulus_text']
        with db.connect() as c:
            runtime.complete_turn_execution(c, turn, True, None, {'execution_receipt': {'output': response.json(), 'capability': cap.name, 'execution_location': cap.execution_location, 'action_type': 'start_diagnostic', 'status': 'completed', 'completed_at': stamp}})
            c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id=?", (turn,)); c.commit()
        # Deliberately different ASR text: it must never become the reference.
        answer = 'Ada visited a garden and asked her teacher for help.'
        voice = capture(client, settings, monkeypatch, sid, answer, prompt, 'reference-original-answer')
        heard = interpret(client, sid, answer, 'reference-answer-turn', voice)
        assert heard['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        confirmation = '刚才那句就是我的答案'
        confirmation_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'reference-confirmation')
        heard = interpret(client, sid, confirmation, 'reference-confirmation-turn', confirmation_voice)
        assert heard['action']['source_voice_input_id'] == voice and voice != confirmation_voice
        saved = submit(client, sid, prompt, heard, answer=answer, key='reference-submit')
        assert saved.status_code == 200, saved.text
        assert saved.json()['status'] == 'answered'
        assert 'local_speaking_candidate' in saved.json(), 'READ_ALOUD_DURABLE_INTENT_REQUIRED'
        public_job = saved.json()['local_speaking_candidate']
        assert public_job['state'] == 'blocked' and public_job['error_code'] == 'REFERENCE_EXECUTOR_UNAVAILABLE'
        assert len(registered) == 1
        job = registered[0]  # The original response intentionally hides internal job IDs.
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=?', (job['job_id'],)).fetchone())
            binding = json.loads(row['binding_json'])
            assert binding['task_family'] == 'read_aloud' and binding['voice_input_id'] == voice
            assert source_text['stimulus_text'] not in row['binding_json'] and answer not in row['binding_json']
            assert binding['reference']['text_sha256'] == hashlib.sha256(source_text['stimulus_text'].encode()).hexdigest()
            assert binding['reference']['text_utf8_bytes'] == len(source_text['stimulus_text'].encode())
            assert binding['reference']['source']['source_id'] == source['source_id']
            assert binding['reference']['diagnostic_item_id'] == item_id and binding['reference']['item_version'] == prompt['item_version']
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0] == 0
        replay = submit(client, sid, prompt, heard, answer=answer, key='reference-submit')
        assert replay.status_code == 200 and replay.json()['local_speaking_candidate']['state'] == 'blocked'
        assert replay.json()['local_speaking_candidate']['error_code'] == 'REFERENCE_EXECUTOR_UNAVAILABLE'
        assert len(registered) == 1
    assert not gateway.requests
    return dict(db=db, settings=settings, gateway=gateway, source=source, original=source_text, draft=draft, item_id=item_id,
                sid=sid, prompt=prompt, row=row, binding=binding, voice=voice, job=job, registered=registered)


def test_original_published_reference_capture_atomic_blocked_intent_reopen(original_read_aloud):
    v = original_read_aloud; db = v['db']; settings = v['settings']
    from home_study_agent.pet_multipa_candidate import prepare_input, candidate_result
    with TestClient(create_app(settings, model_gateway=v['gateway']), headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41910)):
        jobs = SpeakingCandidateJobs(db, AgentRuntimeStore(db), media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
        assert jobs.read(CHILD, v['job']['job_id']) == v['job'] and jobs.process_one() is None
        with pytest.raises(ValueError, match='NOT_FOUND'):
            jobs.read(OTHER, v['job']['job_id'])
        with db.connect() as c:
            args = dict(media_id=v['row']['media_id'], assessment_id=v['row']['assessment_id'], media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
            binding, wav = prepare_input(c, v['sid'], **args)
            assert binding == v['binding'] and hashlib.sha256(wav).hexdigest() == binding['audio']['sha256']
            assert c.execute('SELECT count(*) FROM pet_speaking_candidate_job').fetchone()[0] == 1
            with pytest.raises(ValueError, match='REFERENCE_RESULT_REJECTED'):
                candidate_result(c, v['sid'], binding=binding, result={}, asset_hashes={}, **args)
            assert c.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
            from home_study_agent.pet_oral_source import resolve_read_aloud_reference
            reference, text = resolve_read_aloud_reference(c, CHILD, v['item_id'], v['prompt']['item_version'])
            assert reference == binding['reference'] and text == v['original']['stimulus_text']
            with pytest.raises(ValueError):
                resolve_read_aloud_reference(c, OTHER, v['item_id'], v['prompt']['item_version'])
            with pytest.raises(ValueError):
                resolve_read_aloud_reference(c, CHILD, v['item_id'], v['prompt']['item_version'] + '-changed')
            changed = json.loads(json.dumps(binding)); changed['reference']['text_sha256'] = '0' * 64
            with pytest.raises(ValueError, match='ORIGINAL_BINDING_CHANGED'):
                candidate_result(c, v['sid'], binding=changed, result={}, asset_hashes={}, **args)
            history = [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')]
            for sql, values in (
                ('UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id=?', ('Different unreviewed original', v['item_id'])),
                ('UPDATE knowledge_source SET current_version=0 WHERE source_id=?', (v['source']['source_id'],)),
                ("UPDATE curriculum_supply_package SET state='withdrawn' WHERE candidate_id=?", (v['draft']['candidate_id'],)),
                ("UPDATE child_profile SET grade='五年级' WHERE child_id=?", (CHILD,)),
            ):
                c.execute('SAVEPOINT reference_changed')
                try:
                    c.execute(sql, values)
                    with pytest.raises(ValueError):
                        prepare_input(c, v['sid'], **args)
                finally:
                    c.execute('ROLLBACK TO reference_changed'); c.execute('RELEASE reference_changed')
            assert [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')] == history
            assert dict(c.execute('SELECT * FROM pet_speaking_candidate_job').fetchone()) == v['row']
            assert c.execute('SELECT count(*) FROM pet_speaking_model_candidate').fetchone()[0] == 0
    # Actual existing parent withdrawal changes qualification, never the saved answer.
    with TestClient(create_app(settings, model_gateway=v['gateway']), headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41911)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        withdrawn = post(client, BASE + '/' + v['draft']['candidate_id'] + '/withdraw', dict(action_id='withdraw-reference', expected_version=4, note='Synthetic source withdrawal'))
        assert not withdrawn['eligible']
        with db.connect() as c:
            with pytest.raises(ValueError):
                prepare_input(c, v['sid'], **args)
            assert [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')] == history
            assert dict(c.execute('SELECT * FROM pet_speaking_candidate_job').fetchone()) == v['row']
        assert jobs.process_one() is None

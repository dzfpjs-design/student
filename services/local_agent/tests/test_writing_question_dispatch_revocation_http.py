"""Real HTTP evaluator and ModelGateway egress; all network is MockTransport.

The reused setup publishes a synthetic source and saves an actual native answer;
only its original objective binding is structured test setup, not voice evidence.
"""
import json
import traceback
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_outcome_evaluator import InvalidLearningEvaluationError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
from test_writing_question_authorization import published_question, grant, CHILD
from test_writing_question_authorization_http import host, parent, path
from test_writing_question_evaluator import projection, TEXT


def model_response(body):
    payload = json.loads(body['messages'][1]['content'])
    evaluation = dict(
        understanding='partial', confidence=.8, evidence_summary='Controlled original-response evidence',
        spoken_feedback='Make the invitation time clearer.', next_check='Revise the invitation sentence.',
        rubric_dimensions=[dict(dimension_id=d['dimension_id'], score=2,
            response_quotes=[payload['response']], rationale='Evidence in the original response')
            for d in payload['rubric']['dimensions']],
        criterion_evidence=[dict(criterion=point, response_quote=payload['response'])
            for point in payload['rubric']['task_points']])
    from home_study_agent.learning_outcome_evaluator import FORMATIVE_SYSTEM_PROMPT
    if body['messages'][0]['content']==FORMATIVE_SYSTEM_PROMPT:
        from formative_response_fixture import criterion_reply
        evaluation=criterion_reply(payload,evaluation)
    return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(evaluation)}}],
        'usage':{'prompt_tokens':10, 'completion_tokens':10}})


@pytest.mark.parametrize('change,http_status', [
    ('revoke',500), ('revoke',403), ('privacy_false',500), ('delete',500),
])
def test_actual_gateway_never_reposts_writing_after_parent_revocation(
        published_question, projection, monkeypatch, change, http_status):
    case, scope = projection
    db, sid, prompts, prompt, _ = case
    grant(published_question)
    settings = replace(host(published_question, monkeypatch),
        model_provider='qwen', model_name='isolated-model', model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f',
        model_base_url='https://primary.invalid/v1', fallback_model_provider='deepseek',
        fallback_model_name='isolated-fallback', fallback_model_api_key='synthetic-72a194138a028b0486b3207b',
        fallback_model_base_url='https://fallback.invalid/v1')
    posts = []
    mutations = []
    mutation_errors = []
    application = None
    headers = None
    deletion_body = {'scope':'knowledge','confirmation':'确认删除','client_action_id':'inflight-source-delete'}
    with db.connect() as c:
        source_id = c.execute('SELECT source_id FROM writing_question_authorization_event WHERE child_id=?',
                              (CHILD,)).fetchone()[0]

    async def transport(request):
        body = json.loads(request.content)
        posts.append((request.url.host, body))
        if len(posts) == 1:
            # Revoke while the first provider request is in flight, not before
            # entering the evaluator. Nested ASGI avoids a blocking TestClient.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url='http://testserver') as admin:
                if change == 'revoke':
                    changed = await admin.post(path(published_question)+'/revoke', headers=headers,
                        json={'expected_revision':1,'client_action_id':'inflight-purpose-revoke'})
                elif change == 'privacy_false':
                    changed = await admin.put('/v1/parent/privacy', headers=headers,
                                              json={'cloud_model_text_allowed':False})
                else:
                    try:
                        changed = await admin.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
                            json=deletion_body)
                    except Exception:
                        mutation_errors.append(traceback.format_exc())
                        raise
                expected_status = 409 if change == 'delete' else 200
                if changed.status_code != expected_status:
                    mutation_errors.append(changed.text)
                assert changed.status_code == expected_status, changed.text
                if change == 'delete':
                    assert changed.json()['code'] == 'DELETION_AWAITING_EVALUATION', changed.text
                    with db.connect() as c:
                        job = c.execute('SELECT * FROM deletion_job WHERE client_action_id=?',
                                        (deletion_body['client_action_id'],)).fetchone()
                        assert job and job['status'] != 'completed'
                        assert job['error_code'] == 'DELETION_AWAITING_EVALUATION'
                        assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(source_id,)).fetchone()
                        assert c.execute("SELECT 1 FROM learning_evaluation_execution WHERE state='running'").fetchone()
                mutations.append(changed.json())
            return httpx.Response(http_status, json={'error':{'message':'controlled first response'}})
        # A regression must reach a real successful provider response, rather
        # than being concealed by an invalid response or a missing fallback.
        return model_response(body)

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application, raise_server_exceptions=False) as client:
        headers = parent(client, setup=True)
        current = client.get(path(published_question), headers=headers)
        assert current.status_code == 200 and current.json()['status'] == 'authorized', current.text
        attempt = prompts.read(sid, prompt_id=prompt['prompt_id'])['attempt']
        follow = application.state.agent_runtime.finalize_diagnostic_evaluation(sid, attempt['evidence_id'])
        assert follow['status'] == 'feedback_required', follow
        url = f"/v1/agent-turns/{follow['feedback_turn_id']}/execute"
        response = client.post(url)
        assert mutations, 'Deletion/authorization must commit before a retry: ' + '\n'.join(mutation_errors)
        assert len(posts) == 1, f'Revoked original was retransmitted to {[p[0] for p in posts]}'
        assert response.status_code == 409, response.text
        # A failed/unknown receipt must not become a licence to re-send.
        replay = client.post(url)
        assert replay.status_code in {404,409}, replay.text
        assert len(posts) == 1
        if change == 'delete':
            with db.connect() as c:
                assert c.execute("SELECT 1 FROM learning_evaluation_execution WHERE state='unknown'").fetchone()
                deletion_id = c.execute('SELECT deletion_id FROM deletion_job WHERE client_action_id=?',
                                        (deletion_body['client_action_id'],)).fetchone()[0]
    if change == 'delete':
        # Persisted pending intent survives a complete application restart.
        # The exact original delete key now performs the physical source cleanup.
        application = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
        with TestClient(application, raise_server_exceptions=False) as client:
            headers = parent(client)
            deleted = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers, json=deletion_body)
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()['status'] == 'completed', deleted.text
            assert deleted.json()['deletion_id'] == deletion_id
            again = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers, json=deletion_body)
            assert again.status_code == 200 and again.json()['deletion_id'] == deletion_id, again.text
            late = client.post(url)
            assert late.status_code in {404,409}, late.text
            assert len(posts) == 1
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(source_id,)).fetchone()
            assert not c.execute('SELECT 1 FROM writing_question_authorization_event WHERE child_id=?',(CHILD,)).fetchone()
            assert c.execute('SELECT COUNT(*) FROM deletion_job WHERE client_action_id=?',
                             (deletion_body['client_action_id'],)).fetchone()[0] == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?',(CHILD,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome WHERE child_id=?',(CHILD,)).fetchone()[0] == 0
    sent = posts[0][1]
    payload = json.loads(sent['messages'][1]['content'])
    assert set(payload) == {'question','response','rubric'} and payload['response'] == TEXT
    serialized = json.dumps(sent)
    assert all(value not in serialized for value in (
        CHILD, sid, scope['objective_id'], prompt['prompt_id'], 'local_audit', 'source_snapshot_json',
        'knowledge_node_id', 'license_basis', 'pre_dispatch', 'PRIVATE history'))


@pytest.mark.parametrize('scenario', ['response_ready_deleted', 'other_child_deleted', 'other_child_pending'])
def test_exact_deletion_blocks_late_persist_but_not_another_child_retry(
        published_question, projection, monkeypatch, scenario):
    case, scope = projection
    db, sid, prompts, prompt, _ = case
    grant(published_question)
    other = 'child-junior-3'
    knowledge = KnowledgeBaseService(db)
    other_source = knowledge.import_source(other, dict(source_type='school_material',
        license_status='public', parent_reviewed=True, title='Other child retained source',
        subject='数学', grade_band='初三', content_text='Other child original source content.'))
    other_before = knowledge.get_source(other_source['source_id'])
    settings = replace(host(published_question, monkeypatch), model_provider='qwen',
        model_name='isolated-model', model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f', model_base_url='https://primary.invalid/v1')
    posts, deleted = [], []
    application = None
    headers = None
    if scenario == 'other_child_pending':
        original_delete = PrivacyLifecycleService._execute_child_scope_deletion_locked

        def other_child_storage_fault(self, deletion_id):
            with db.connect() as c:
                child = c.execute('SELECT child_id FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone()[0]
            if child == other:
                raise PrivacyEvidenceCleanupError('controlled other-child cleanup failure')
            return original_delete(self, deletion_id)

        monkeypatch.setattr(PrivacyLifecycleService, '_execute_child_scope_deletion_locked', other_child_storage_fault)

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        if scenario.startswith('other_child_') and len(posts) == 1:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url='http://testserver') as admin:
                result = await admin.post(f'/v1/parent/children/{other}/delete', headers=headers,
                    json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'delete-other-during-retry'})
                if scenario == 'other_child_pending':
                    assert result.status_code == 500 and result.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', result.text
                    with db.connect() as c:
                        job = c.execute('SELECT child_id,status FROM deletion_job WHERE client_action_id=?',
                                        ('delete-other-during-retry',)).fetchone()
                        assert job['child_id'] == other and job['status'] != 'completed'
                else:
                    assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
                deleted.append(result.json())
            return httpx.Response(500, json={'error':{'message':'controlled retry'}})
        return model_response(body)

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application, raise_server_exceptions=False) as client:
        headers = parent(client, setup=True)
        evaluator = application.state.learning_outcome_evaluator
        persist = evaluator._persist
        if scenario == 'response_ready_deleted':
            def interrupted(*args):
                raise RuntimeError('controlled storage interruption after saved provider response')
            monkeypatch.setattr(evaluator, '_persist', interrupted)
        attempt = prompts.read(sid, prompt_id=prompt['prompt_id'])['attempt']
        follow = application.state.agent_runtime.finalize_diagnostic_evaluation(sid, attempt['evidence_id'])
        assert follow['status'] == 'feedback_required', follow
        url = f"/v1/agent-turns/{follow['feedback_turn_id']}/execute"
        result = client.post(url)
        if scenario.startswith('other_child_'):
            assert deleted and len(posts) == 2
            assert result.status_code == 200, result.text
            assert client.get(path(published_question), headers=headers).json()['status'] == 'authorized'
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?',(CHILD,)).fetchone()[0] == 1
                assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?',(other,)).fetchone()[0] == 0
                present = c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(other_source['source_id'],)).fetchone()
                assert bool(present) == (scenario == 'other_child_pending')
            assert client.post(url).status_code == 200 and len(posts) == 2
            return
        assert result.status_code == 409, result.text
        assert len(posts) == 1
        with db.connect() as c:
            ready = c.execute("SELECT * FROM learning_evaluation_execution WHERE state='response_ready'").fetchone()
            assert ready
            record, evaluation = json.loads(ready['record_json']), json.loads(ready['evaluation_json'])
            original_answer = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
                                             (attempt['attempt_id'],)).fetchone())
        deletion = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'delete-saved-response-source'})
        assert deletion.status_code == 200 and deletion.json()['status'] == 'completed', deletion.text
        monkeypatch.setattr(evaluator, '_persist', persist)
        with pytest.raises(InvalidLearningEvaluationError, match='SOURCE_DELETED'):
            persist(record, evaluation, ready['model_call_id'])
        # Even a legacy saved record without typed projection needs the same gate.
        legacy = {key:value for key,value in record.items() if key != 'formative_projection'}
        with pytest.raises(InvalidLearningEvaluationError, match='SOURCE_DELETED'):
            persist(legacy, evaluation, ready['model_call_id'])
        assert client.post(url).status_code in {404,409}
        assert len(posts) == 1
        assert knowledge.get_source(other_source['source_id']) == other_before
        with db.connect() as c:
            tombstone = c.execute('SELECT * FROM learning_evaluation_execution WHERE step_id=?', (record['step_id'],)).fetchone()
            assert tombstone and tombstone['state'] == 'unknown'
            assert c.execute('SELECT error_code FROM agent_step WHERE step_id=?',(record['step_id'],)).fetchone()[0] == 'SOURCE_DELETED'
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?',(CHILD,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM agent_outcome WHERE child_id=?',(CHILD,)).fetchone()[0] == 0
            after = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
            assert after and after['submitted_answer'] == original_answer['submitted_answer'] == TEXT
            assert after['created_at'] == original_answer['created_at']

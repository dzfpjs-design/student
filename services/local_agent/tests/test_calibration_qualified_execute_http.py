"""Qualified-score host plumbing, not evidence of deployed calibration.

The 200-case historical corpus and its identity authority are synthetic same-DB
test dependencies. The new answer/HTTP execution uses the real evaluator and
ModelGateway; no HTTP body can install the dependency in production. No fixture
changes source scoring modes or claims a physical voice/independent performance.
"""
from datetime import datetime, timedelta, timezone
import json

import httpx
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output, context_manifest
from home_study_agent.config import Settings
from home_study_agent.learning_outcome_evaluator import STANDARD_SYSTEM_PROMPT, PROMPT_VERSION
from home_study_agent.model_gateway import ModelGateway, ModelEndpoint, _scorer_request_provenance
from home_study_agent import rubric_calibration_authority, learning_rubric
from home_study_agent.rubric_calibration_evidence import digest
from home_study_agent.rubric_calibration import RubricCalibrationService
from test_mistake_gate_reliability import flow, CHILD
from test_rubric_calibration import Corpus


def qualified_history(flow, monkeypatch, settings):
    flow[3][0] = datetime.now(timezone.utc) - timedelta(hours=1)
    corpus = Corpus(flow, monkeypatch, count=200)
    endpoint = ModelEndpoint(settings.model_provider, None, settings.model_base_url, settings.model_name)
    provenance = _scorer_request_provenance(endpoint, ModelGateway.request_payload(endpoint,
        [{'role': 'system', 'content': STANDARD_SYSTEM_PROMPT}], 'learning_outcome_evaluation'))
    corpus.scorer.update(provider=settings.model_provider, model=settings.model_name,
        prompt_hash=provenance['system_prompt_sha256'], parameters_hash=provenance['parameters_sha256'],
        parser_version=PROMPT_VERSION, projection_version=PROMPT_VERSION)
    corpus.scope['task_family'] = 'math_process'
    corpus.holdout.update(scope_hash=digest(corpus.scope), application_scope=[
        {'profile_id': 'math_process', 'answer_types': ['text'], 'stratum': 'ordinary'}])
    corpus.revision.update(provider=settings.model_provider, model=settings.model_name,
                           endpoint_scope_sha256=provenance['endpoint_scope_sha256'])
    corpus.save('holdout', corpus.holdout)
    corpus.save('scorer_revision', corpus.revision)
    corpus.freeze()
    for i in range(200):
        aid, labels = corpus.score(i, score=i//50, gold=i//50,
            provenance_override={'endpoint_scope_sha256': provenance['endpoint_scope_sha256']})
        corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
            expected_revision=i+1, client_action_id='case-' + str(i))
    assert corpus.service.evaluate(CHILD, corpus.pid, expected_revision=201,
                                   client_action_id='qualify')['current']['score_qualified']
    original = rubric_calibration_authority.resolve

    def historical_authority(c, *, child_id, kind, reference_id):
        if kind == 'assessment_scorer':
            actual = original(c, child_id=child_id, kind=kind, reference_id=reference_id)
            if actual:
                return actual
        return corpus.resolve(c, child_id=child_id, kind=kind, reference_id=reference_id)
    monkeypatch.setattr(rubric_calibration_authority, 'resolve', historical_authority)
    # Native ReadScope deliberately resolves signed journals directly. Install
    # this test's unsigned historical dependency through the explicit service
    # resolver too; do not make production trust arbitrary patched resolvers.
    initialize = RubricCalibrationService.__init__
    def with_historical_dependency(self, database, *, authority_resolver=None, clock=None):
        initialize(self, database, authority_resolver=authority_resolver or historical_authority, clock=clock)
    monkeypatch.setattr(RubricCalibrationService, '__init__', with_historical_dependency)
    return corpus


def test_actual_qualified_nonholdout_execute_projects_conditions_and_revoke_after_restart(flow, monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=flow[0].path, maintenance_enabled=False,
        model_provider='qwen', model_name='isolated-model', model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd',
        model_base_url='https://isolated.invalid/v1')
    corpus = qualified_history(flow, monkeypatch, settings)
    corpus.clock[0] = datetime.now(timezone.utc)
    monkeypatch.setattr(learning_rubric, 'datetime', datetime)
    posts = []

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        context = json.loads(body['messages'][1]['content'])
        result = {'understanding': 'independent', 'confidence': .92, 'evidence_summary': 'Synthetic transport observation',
            'spoken_feedback': '把运算的依据也写出来。', 'next_check': '再解释计算依据。',
            'criterion_evidence': [], 'rubric_dimensions': [{
                'dimension_id': d['dimension_id'], 'score': 3, 'response_quotes': [context['child_response']],
                'rationale': 'Synthetic model score, not deployed quality'} for d in context['rubric_dimensions']]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})

    def application():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))

    app = application()
    with TestClient(app, client=('127.0.0.1', 41000), raise_server_exceptions=False) as client:
        headers = {'X-HSC-Parent': client.post('/v1/parent/setup', json={'pin': '7362'}).json()['parent_token']}
        assert client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': True}).status_code == 200
        # Actual source submission. The same question was seen in corpus setup:
        # this new response must NOT become independent merely because it scores.
        fresh = corpus.learning.submit_diagnostic(corpus.sid, corpus.item_ids[200], '90', 'fresh-answer')
        turn = 'calibration-new-answer'
        runtime = app.state.agent_runtime
        with corpus.db.connect() as c:
            question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (corpus.item_ids[200],)).fetchone()[0]
            objective = c.execute('SELECT * FROM agent_objective WHERE objective_id=?', (corpus.objective,)).fetchone()
        decision = parse_agent_plan_output(json.dumps({
            'objective': {'mode': 'continue', 'type': 'learning', 'summary': objective['summary'],
                'success_criteria': json.loads(objective['success_criteria_json']), 'phase': objective['phase']},
            'next_step': {'capability': 'learning.evaluate_response', 'arguments': {
                'response': '90', 'question_asked': question, 'subject': '数学', 'evidence_id': fresh['evidence_id']},
                'reason': 'Evaluate this exact new response', 'expected_evidence': ['independent_response']},
            'spoken_transition': '看看这次回答。', 'requires_child_reply': True, 'confidence': .9}), 'study', CapabilityRegistry())
        manifest = context_manifest(runtime.assemble_context(corpus.sid, ()))
        with corpus.db.connect() as c:
            now = datetime.now(timezone.utc).isoformat()
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id)
                VALUES (?,?,?,'90','learning.evaluate_response','看看这次回答。','{}','agent_runtime_planner_v1',?,?)''',
                (turn, CHILD, corpus.sid, now, turn))
            runtime.persist_plan(c, child_id=CHILD, session_id=corpus.sid, agent_turn_id=turn,
                client_action_id=turn, decision=decision, context_manifest=manifest)
            c.commit()
        response = client.post('/v1/agent-turns/' + turn + '/execute')
        assert response.status_code == 200, response.text
        assert len(posts) == 1
        with corpus.db.connect() as c:
            row = c.execute('SELECT * FROM rubric_assessment ORDER BY rowid DESC LIMIT 1').fetchone()
            payload = json.loads(row['payload_json'])
            assert payload['source_evidence_id'] == fresh['evidence_id']
            assert payload['calibration_execution'].get('mode') == 'score', payload['calibration_execution']
            assessment_id = row['assessment_id']
        url = f'/v1/parent/children/{CHILD}/learning-rubrics/{assessment_id}'
        view = client.get(url, headers=headers)
        assert view.status_code == 200, view.text
        qualification = view.json()['qualification']
        assert qualification['receipt']['score_status'] == 'calibrated_model', qualification
        assert not qualification['eligible_for_mastery_gate']
        assert not qualification['receipt']['independent']
        protocols = client.get(f'/v1/parent/children/{CHILD}/rubric-calibration', headers=headers)
        assert protocols.status_code == 200, protocols.text
        assert protocols.json()['requirements']['production_authority_available']
        assert app.state.learning_outcome_evaluator.current_mastery_gate(corpus.objective)['outcome_level'] not in {'independent', 'transferred', 'retained'}
    with TestClient(application(), client=('127.0.0.1', 41000), raise_server_exceptions=False) as client:
        headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin': '7362'}).json()['parent_token']}
        replay = client.post('/v1/agent-turns/' + turn + '/execute')
        assert replay.status_code == 200, replay.text
        assert len(posts) == 1
        assert client.get(url, headers=headers).json()['qualification']['receipt']['assessment_id'] == assessment_id
        revoked = client.post(f'/v1/parent/children/{CHILD}/rubric-calibration/{corpus.pid}/revoke', headers=headers,
                              json={'expected_revision': 202, 'client_action_id': 'withdraw-certificate'})
        assert revoked.status_code == 200, revoked.text
        assert client.get(url, headers=headers).json()['qualification']['receipt'] is None
        assert not client.get(f'/v1/parent/children/{CHILD}/rubric-calibration', headers=headers).json()['requirements']['production_authority_available']
        with corpus.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 202
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 201

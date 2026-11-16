"""An ineligible saved draft must not prevent acknowledgement or backend startup."""
import json
import pytest
from test_scheduled_writing_natural_runtime_http import (
    scheduled, post, TEXT, WritingOriginalService, _http_client, create_app)
from home_study_agent.agent_runtime import AgentRuntimeStore


@pytest.mark.parametrize('capture', [False, True])
def test_unbound_written_answer_is_saved_but_cannot_crash_restart(tmp_path, capture):
    with scheduled(tmp_path) as case:
        p=case['prompt']
        if capture:
            WritingOriginalService(case['db']).capture_native_response(case['sid'],prompt_id=p['prompt_id'],
                item_version=p['item_version'],text=TEXT,client_action_id='native-not-submitted')
        with case['db'].connect() as c:
            objectives=[dict(r) for r in c.execute('SELECT * FROM agent_objective')]
        body=dict(prompt_id=p['prompt_id'],item_version=p['item_version'],answer=TEXT,client_action_id='ui-submit')
        result=post(case['client'],f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',body)
        assert result['status']=='answered'
        assert result['local_learning_evaluation']['status']=='blocked'
        assert result['local_learning_evaluation']['reason'].startswith('WRITING_ORIGINAL_')
        attempt=result['attempt']
        calls=len(case['transport'].calls)
        with case['db'].connect() as c:
            assert [dict(r) for r in c.execute('SELECT * FROM agent_objective')]==objectives
            saved_attempts=[dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
            turns=[dict(r) for r in c.execute('SELECT * FROM agent_turn')]
            assert len(turns)==1, 'Failed reference validation must roll back the pending evaluator'
    with _http_client(create_app(case['settings'],model_gateway=case['gateway'])) as client:
        resumed=client.get(f'/v1/sessions/{case["sid"]}/diagnostic-prompt').json()
        assert all(attempt[k]==v for k,v in resumed['attempt'].items())
        repeated=post(client,f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',body)
        assert repeated['attempt']['attempt_id']==attempt['attempt_id'] and repeated['local_learning_evaluation']['status']=='blocked'
        assert len(case['transport'].calls)==calls
        with case['db'].connect() as c:
            assert [dict(r) for r in c.execute('SELECT * FROM agent_objective')]==objectives
            assert [dict(r) for r in c.execute('SELECT * FROM agent_turn')]==turns
            assert [dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]==saved_attempts
            assert not c.execute('SELECT 1 FROM rubric_assessment').fetchone()
            assert not c.execute('SELECT 1 FROM rubric_review_event').fetchone()
            assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_reference_unexpected_programming_error_is_not_masked(tmp_path,monkeypatch):
    with scheduled(tmp_path) as case:
        def broken(*a,**k):
            raise ValueError('unexpected implementation failure')
        monkeypatch.setattr(AgentRuntimeStore,'prepare_writing_reference_feedback',broken)
        p=case['prompt']
        response=case['client'].post(f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',json=dict(
            prompt_id=p['prompt_id'],item_version=p['item_version'],answer=TEXT,client_action_id='unexpected'))
        assert response.status_code==500

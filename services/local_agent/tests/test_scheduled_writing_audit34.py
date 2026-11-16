"""Independent scheduled-writing authority audit; real TCP, synthetic transport."""
from pathlib import Path
import json
import socket

import httpx
import pytest
from home_study_agent.formative_evaluation_projection import build_formative_projection
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.app import create_app
from test_scheduled_writing_natural_runtime_http import scheduled, native_answer, post, CHILD, ControlledTransport
from test_school_review_start_integrity_http import _http_client
from home_study_agent.repository import StudyRepository, utc_now


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    import home_study_agent.scheduled_writing_goal as module
    assert Path(__file__).resolve().parents[1] / 'src' in Path(module.__file__).resolve().parents
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1'}, 'External socket forbidden'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


def projection(c, case):
    oid = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
        (case['planned']['agent_turn_id'],)).fetchone()[0]
    return build_formative_projection(c, child_id=CHILD, session_id=case['sid'],
        prompt_id=case['prompt']['prompt_id'], item_version=case['prompt']['item_version'], objective_id=oid)


@pytest.mark.parametrize('fault', ['failed_turn', 'deleted_turn', 'wrong_child_receipt', 'missing_receipt'])
def test_start_receipt_damage_cannot_authorize_scoring(tmp_path, fault):
    with scheduled(tmp_path) as case:
        answer, _ = native_answer(case)
        with case['db'].connect() as c:
            assert projection(c, case)['status'] == 'ready_for_runtime_checks'
            tid = case['planned']['agent_turn_id']
            if fault == 'failed_turn':
                c.execute("UPDATE agent_turn SET tool_status='failed' WHERE agent_turn_id=?", (tid,))
            elif fault == 'deleted_turn':
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (tid,))
            else:
                step = c.execute('SELECT * FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (tid,)).fetchone()
                result = json.loads(step['result_json'])
                if fault == 'wrong_child_receipt':
                    result['result']['diagnostic_prompt']['prompt']['child_id'] = 'child-junior-3'
                else:
                    result = {}
                c.execute('UPDATE agent_step SET result_json=? WHERE step_id=?', (json.dumps(result), step['step_id']))
            c.commit()
            value = projection(c, case)
        calls = len(case['transport'].calls)
        response = case['client'].post(f'/v1/agent-turns/{answer["local_learning_evaluation"]["feedback_turn_id"]}/execute', json={})
        assert response.status_code in {200,409,422}, response.text
        assert len(case['transport'].calls) == calls, (fault, value, response.text)
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
        assert value['status'] == 'local_required', (fault, value)


def test_plain_pause_resume_after_answer_keeps_goal_and_one_assessment_across_restart(tmp_path):
    with scheduled(tmp_path) as case:
        answer, body = native_answer(case)
        with case['db'].connect() as c:
            before = dict(c.execute('SELECT * FROM agent_objective WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)',
                (case['planned']['agent_turn_id'],)).fetchone())
        for control in ['pause', 'resume']:
            post(case['client'], f'/v1/plan-items/{case["pid"]}/{control}', {'client_action_id':'ordinary34-' + control})
        with case['db'].connect() as c:
            after = dict(c.execute('SELECT * FROM agent_objective WHERE objective_id=?', (before['objective_id'],)).fetchone())
            assert after == before
            assert projection(c, case)['status'] == 'ready_for_runtime_checks'
        follow = answer['local_learning_evaluation']['feedback_turn_id']
        evaluated = post(case['client'], f'/v1/agent-turns/{follow}/execute', {})
        assert evaluated['execution']['result']['rubric_assessment']['source_evidence_id'] == answer['attempt']['evidence_id']
        with case['db'].connect() as c:
            original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (answer['attempt']['attempt_id'],)).fetchone())
    transport = ControlledTransport()
    with _http_client(create_app(case['settings'], model_gateway=ModelGateway(case['settings'], transport=httpx.MockTransport(transport)))) as client:
        assert not transport.calls
        replay = post(client, f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer', body)
        assert replay['attempt']['attempt_id'] == original['attempt_id']
        post(client, f'/v1/agent-turns/{follow}/execute', {})
        assert not transport.calls
        with case['db'].connect() as c:
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (original['attempt_id'],)).fetchone()) == original
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1


def test_legacy_shaped_original_in_ended_session_never_claims_active_goal(tmp_path):
    with scheduled(tmp_path) as case:
        answer, _ = native_answer(case)
        with case['db'].connect() as c:
            # Portable legacy-shape control; the separate audit also uses an
            # actual pre-upgrade process DB rather than this metadata fixture.
            c.execute('''UPDATE agent_plan SET plan_json=json_remove(plan_json,'$.local_learning_binding'),
                context_manifest_json=json_remove(context_manifest_json,'$.scheduled_writing_goal')
                WHERE agent_turn_id=?''', (case['planned']['agent_turn_id'],))
            c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id=?", (utc_now(), case['sid']))
            c.execute("UPDATE agent_objective SET status='paused' WHERE session_id=?", (case['sid'],))
            c.commit()
            original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (answer['attempt']['attempt_id'],)).fetchone())
            before = c.execute('SELECT COUNT(*) FROM agent_objective').fetchone()[0]
        next_sid = StudyRepository(case['db']).start_session(CHILD, 'next-session34')['session_id']
        recovered = case['client'].app.state.agent_runtime.recover_learning_outcomes()
        with case['db'].connect() as c:
            assert not c.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND session_id!=? AND status='active'", (CHILD, next_sid)).fetchone(), recovered
            assert c.execute('SELECT COUNT(*) FROM agent_objective').fetchone()[0] == before
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (original['attempt_id'],)).fetchone()) == original
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0


def test_start_source_withdrawn_during_model_response_does_not_commit_assessment(tmp_path, monkeypatch):
    original = ControlledTransport.__call__
    holder = {}
    async def changed(transport, request):
        response = await original(transport, request)
        body = json.loads(request.content)
        supplied = json.loads(body['messages'][-1]['content'])
        if set(supplied) == {'question','response','rubric'}:
            case = holder['case']
            with case['db'].connect() as c:
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (case['planned']['agent_turn_id'],))
                c.commit()
        return response
    monkeypatch.setattr(ControlledTransport, '__call__', changed)
    with scheduled(tmp_path) as case:
        holder['case'] = case
        answer, _ = native_answer(case)
        response = case['client'].post(f'/v1/agent-turns/{answer["local_learning_evaluation"]["feedback_turn_id"]}/execute', json={})
        assert response.status_code in {200,409,422}, response.text
        assert len(case['transport'].calls) == 2
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0, response.text

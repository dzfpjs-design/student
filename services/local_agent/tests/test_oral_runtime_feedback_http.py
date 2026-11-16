"""Real local HTTP dispatch; synthetic PCM/recognizer and no-network semantics."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.model_gateway import ModelResult
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import start
from test_oral_source_revision_combined_http import author
from test_pet_oral_supply_http import setup, post, CHILD, OTHER, BASE
from test_voice_runtime_binding import capture, interpret, submit


ANSWER = 'I want to visit the garden because it is quiet. I would walk there with my family and look at the flowers.'


class OralGateway:
    configured = True

    def __init__(self):
        self.requests = []
        self.points = []
        self.quote = '我想练习口语，回答接下来的题目'

    async def complete(self, request):
        self.requests.append(request)
        if getattr(self,'ordinary_chat',False):
            if request.request_type=='voice_interaction':
                value={'role':'unknown','control':None,'quote':self.quote,'confidence':.99}
            else:
                assert request.request_type=='agent_plan'
                value=Planner('conversation.respond',{'response':'体育课里有让你开心的事呀。'},self.quote,role='companionship').payload
                value['objective']={'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
                value['spoken_transition']='体育课里有让你开心的事呀。'
            return ModelResult(content=json.dumps(value),provider='fixture',model='fixture',latency_ms=1)
        if request.request_type == 'agent_plan':
            value = Planner('diagnostic.start', {}, self.quote, objective_type='learning').payload
            value['objective'].update(phase='diagnose', success_criteria=self.points)
            value['requires_child_reply'] = True
        else:
            assert request.request_type == 'voice_interaction', 'Oral reference must not invoke a cloud evaluator'
            if getattr(self,'requested_control',None):
                # Respect the current classifier's declared enum. An absent
                # oral control is legal unknown, not a fabricated invalid reply.
                declared = self.requested_control in json.dumps(request.messages,ensure_ascii=False)
                value = {'role':'control' if declared else 'unknown',
                    'control':self.requested_control if declared else None,'quote':self.quote,'confidence':.99}
            else:
                value = {'role': 'answer', 'control': None, 'quote': self.quote, 'confidence': .99}
        return ModelResult(content=json.dumps(value), provider='fixture', model='fixture', latency_ms=1)


def begin_oral(client, db, gateway):
    original, _, _, _, published = author(client, db, legacy=False)
    gateway.points = original['task_points']
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    requested = post(client, f'/v1/sessions/{sid}/agent-turn', dict(
        transcript=gateway.quote, client_action_id='oral-start-goal', cloud_text_allowed=True))
    assert requested['runtime_capability'] == 'diagnostic.start', requested
    ready = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare', dict(
        track='pet', preferred_item_id=published['diagnostic_item_id'], client_action_id='oral-original-prompt'))
    presented = client.put(f"/v1/agent-turns/{requested['agent_turn_id']}/outcome", json={
        'tool_status': 'completed', 'final_response': '原题已返回，等待孩子作答。',
        'result': {'execution_receipt': {'capability': 'diagnostic.start', 'execution_location': 'client_device_bridge',
            'action_type': 'start_diagnostic', 'status': 'completed',
            'completed_at': datetime.now(timezone.utc).isoformat(), 'output': ready}}})
    assert presented.status_code == 200, presented.text
    return sid, ready['prompt'], original, published


def review_original(client, result, original):
    assessment = result['result']['rubric_assessment']
    path = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}"
    response = client.get(path)
    assert response.status_code == 200, response.text
    before = response.json()
    binding = before['audio_review']['bindings'][0]
    raw = client.get(path + '/audio', params={'media_id': binding['media_id'], 'audio_sha256': binding['audio']['sha256']})
    assert raw.status_code == 200 and raw.headers['cache-control'] == 'no-store'
    body = dict(expected_revision=0, client_action_id='oral-current-low-review', operation='approve',
        reason='Synthetic fixture review of exact original audio; no physical speech-quality claim.',
        independent=True, hint_level=0, review_profile='pet_speaking_audio_v1',
        audio_evidence={'media_id': binding['media_id'], 'review': {
            'schema': 'pet_speaking_audio_references_v1', 'binding_version': binding['binding_version'],
            'audio_sha256': binding['audio']['sha256'], 'observations': [dict(dimension_id=name,
                start_frame=0, end_frame=binding['audio']['frames'], heard_text='Synthetic PCM annotation.',
                rationale='Exact original-frame transport fixture.') for name in binding['dimensions_required']]}},
        dimensions=[dict(dimension_id=d['dimension_id'], score=0,
            response_quotes=[ANSWER] if d['dimension_id'] in {'task','coherence','language'} else [],
            rationale='Try connecting your chosen place to one concrete reason.') for d in before['audio_review']['dimensions']],
        criterion_evidence=[dict(criterion=point,response_quote=ANSWER) for point in original['task_points']])
    approved = post(client,path+'/review',body)
    assert not approved['qualification']['eligible_for_mastery_gate']
    return path, body, approved


@pytest.mark.parametrize('fault', ['none','review_retract','source','cancel','new_prompt','child','request_retry','direct'])
def test_actual_original_answer_delivers_specific_oral_feedback_and_restart(tmp_path, monkeypatch, fault):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'J' * 32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    gateway = OralGateway()
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers, client=('127.0.0.1', 41940)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True,
            'cloud_model_text_allowed': True, 'cloud_audio_allowed': False}).status_code == 200
        sid, prompt, original, published = begin_oral(client, db, gateway)
        gateway.quote = ANSWER
        voice_id = capture(client, settings, monkeypatch, sid, ANSWER, prompt, 'oral-original-audio')
        heard = interpret(client, sid, ANSWER, 'oral-original-answer', voice_id)
        if heard['intent'] == 'pending_answer_clarification':
            post(client, f"/v1/agent-turns/{heard['agent_turn_id']}/execute", {})
            confirmation = '刚才那句就是我的答案'
            gateway.quote = confirmation
            confirmed = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'oral-role-confirmation')
            heard = interpret(client, sid, confirmation, 'oral-role-confirmed', confirmed)
        assert heard['action']['type'] == 'submit_diagnostic_answer', heard
        assert heard['action']['source_voice_input_id'] == voice_id
        saved = submit(client, sid, prompt, heard, answer=ANSWER, key='oral-original-submit')
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body['attempt']['submitted_answer'] == ANSWER
        # Existing PET keeps a heuristic reference value; qualification, not
        # that numeric value, determines whether it can establish learning.
        assert body['attempt']['score_status'] == 'needs_review'
        with db.connect() as c:
            evidence = c.execute('SELECT independent FROM learning_evidence WHERE evidence_id=?',
                (body['attempt']['evidence_id'],)).fetchone()
            assert not evidence['independent']
        follow = body['local_learning_evaluation']
        assert follow['status'] == 'feedback_ready', follow
        tid = follow['feedback_turn_id']
        feedback = client.get(f'/v1/agent-turns/{tid}/execution-status')
        assert feedback.status_code == 200, feedback.text
        assert feedback.json()['status'] == 'completed', feedback.text
        result = feedback.json()['execution']['execution']
        assert original['task_points'][0] in result['response_text']
        assert '你想补充一句吗' in result['response_text']
        assert result['result']['oral_feedback']['mastery_claim_allowed'] is False
        assert result['result']['feedback_delivery_allowed'] is True
        assert result['result']['objective_update_applied'] is False
        if fault == 'direct':
            direct = post(client, f'/v1/agent-turns/{tid}/execute', {})
            assert direct['execution']['result']['feedback_delivery_allowed'] is True
            assert direct['execution']['response_text'] == result['response_text']
            with db.connect() as c:
                step = c.execute('''SELECT s.result_json,p.status FROM agent_step s JOIN agent_plan p USING(plan_id)
                    WHERE p.agent_turn_id=?''', (tid,)).fetchone()
                assert step['status'] == 'completed', dict(step)
        with db.connect() as c:
            c.execute('BEGIN')
            retry = AgentRuntimeStore.resolve_oral_followup(c,child_id=CHILD,session_id=sid,
                prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],kind='retry')
            assert retry['status'] == 'ready', retry
            assert retry['action']['required_hint_level'] == 1
            assert retry['action']['parent_prompt_id'] == prompt['prompt_id']
        path, review_body, approved = review_original(client,result,original)
        reviewed = client.get(f'/v1/agent-turns/{tid}/execution-status').json()['execution']['execution']
        assert 'one concrete reason' in reviewed['response_text'], reviewed
        assert reviewed['result']['oral_feedback']['status'] == 'reviewed_audio_feedback'
        assert reviewed['result']['oral_feedback']['review_ref']['review_revision'] == 1
        assert reviewed['result']['feedback_delivery_allowed'] is True
        assert reviewed['result']['objective_update_applied'] is False
        with db.connect() as c:
            c.execute('BEGIN')
            assert AgentRuntimeStore.revalidate_oral_followup(c,action=retry['action'],
                binding_version=retry['binding_version'])['status'] == 'blocked'
        calls = len(gateway.requests)
        replay = submit(client, sid, prompt, heard, answer=ANSWER, key='oral-original-submit')
        assert replay.status_code == 200 and replay.json()['local_learning_evaluation']['feedback_turn_id'] == tid
        assert len(gateway.requests) == calls
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers, client=('127.0.0.1', 41941)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        restored = client.get(f'/v1/agent-turns/{tid}/execution-status')
        assert restored.status_code == 200, restored.text
        assert restored.json()['execution']['execution']['response_text'] == reviewed['response_text']
        assert len(gateway.requests) == calls
        if fault == 'request_retry':
            gateway.quote = '我想按刚才的建议补充一下，再说一次'
            gateway.requested_control = 'oral.retry'
            retry_voice = capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'oral-explicit-retry-audio')
            requested = interpret(client,sid,gateway.quote,'oral-explicit-retry',retry_voice)
            assert requested.get('runtime_capability') == 'oral.retry', requested
            executed = post(client,f"/v1/agent-turns/{requested['agent_turn_id']}/execute",{})
            new_prompt = executed['execution']['result']['prompt']
            assert new_prompt['prompt_id'] != prompt['prompt_id']
            assert new_prompt['diagnostic_item_id'] == prompt['diagnostic_item_id']
            assert executed['execution']['result']['required_hint_level'] >= 1
            return
        if fault == 'review_retract':
            post(client,path+'/review',dict(expected_revision=1,client_action_id='oral-review-retract',
                operation='retract',reason='Synthetic review correction.'))
            assert post(client,path+'/review',review_body)['event'] == approved['event']
        elif fault == 'source':
            post(client,BASE+'/'+published['candidate_id']+'/withdraw',dict(action_id='oral-withdraw',
                expected_version=published['version'],note='Synthetic source withdrawal.'))
        elif fault == 'cancel':
            post(client,f'/v1/sessions/{sid}/agent-turn',dict(transcript='别讲了',client_action_id='oral-stop',cloud_text_allowed=False))
        elif fault == 'new_prompt':
            post(client,f'/v1/sessions/{sid}/diagnostic-prompt/prepare',dict(track='pet',
                preferred_item_id=published['diagnostic_item_id'],client_action_id='a-new-oral-prompt'))
        elif fault == 'child':
            post(client,f'/v1/sessions/{sid}/switch-child',dict(target_child_id=OTHER,client_action_id='oral-other-child'))
        current = client.get(f'/v1/agent-turns/{tid}/execution-status').json()['execution']['execution']
        if fault == 'review_retract':
            assert current['result']['feedback_delivery_allowed'] is True
            assert current['result']['oral_feedback']['review_ref'] is None
            assert original['task_points'][0] in current['response_text']
            assert 'one concrete reason' not in current['response_text']
        elif fault not in {'none','direct'}:
            assert current['result']['feedback_delivery_allowed'] is False, current
            assert 'one concrete reason' not in current['response_text']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
            assert not c.execute("SELECT 1 FROM model_call WHERE purpose='learning_outcome_evaluation'").fetchone()

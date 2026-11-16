"""Actual ASR/OCR/HTTP/dispatcher; recognition and semantic transport are synthetic."""
import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService
from test_diagnostic_prompt_http import environment, start, OTHER
from test_paper_writing_runtime_http import PaperGateway, synthetic_page
from test_voice_runtime_binding import capture, interpret
from test_writing_followup_paper_http import revised_page
from test_writing_voice_runtime_http import writing_prompt, readback_receipt
from test_cloud_first_agent import Planner
from home_study_agent.model_gateway import ModelResult


class JourneyGateway(PaperGateway):
    async def complete(self, request):
        if self.control == 'planning.describe':
            if request.request_type == 'agent_plan':
                self.requests.append(request)
                return await Planner('planning.describe', {}, self.quote).complete(request)
            if request.request_type == 'voice_interaction':
                self.requests.append(request)
                return ModelResult(content=json.dumps({'role': 'question', 'control': None,
                    'quote': self.quote, 'confidence': .99}), provider='fixture', model='fixture', latency_ms=1)
        return await super().complete(request)


class PaperJourney:
    def __init__(self, settings, db, monkeypatch):
        self.settings, self.db, self.monkeypatch = settings, db, monkeypatch
        self.gateway = JourneyGateway()
        def no_page_upload(*args, **kwargs):
            raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
        monkeypatch.setattr(DocumentVisionService, 'analyze_page', no_page_upload)

    def say(self, client, text, key, control, target):
        self.gateway.quote, self.gateway.control = text, control
        voice = capture(client, self.settings, self.monkeypatch, self.sid, text, target, key+'-voice')
        return interpret(client, self.sid, text, key, voice)

    def begin(self, client, *, item_id=None):
        parent = client.post('/v1/parent/setup', json={'pin': '8264'})
        assert parent.status_code == 200, parent.text
        self.headers = {'X-HSC-Parent': parent.json()['parent_token']}
        privacy = client.put('/v1/parent/privacy', headers=self.headers, json={'cloud_model_text_allowed': True})
        assert privacy.status_code == 200, privacy.text
        self.sid = start(client)
        if item_id is None:
            self.prompt = writing_prompt(client, self.db, self.sid)
        else:
            from test_diagnostic_prompt_http import prepare
            with self.db.connect() as c:
                c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (self.sid,))
                c.commit()
            self.prompt = prepare(client, self.sid, track='pet', preferred_item_id=item_id)['prompt']
        monitor = client.post(f'/v1/sessions/{self.sid}/document-monitor/start', json={'client_action_id': 'core-monitor'})
        assert monitor.status_code == 200, monitor.text

    def submit(self, client, target, prefix, png, manuscript):
        artifact = client.post(f'/v1/sessions/{self.sid}/document-monitor/ocr', json={
            'image_base64': base64.b64encode(png).decode(), 'client_action_id': prefix+'-ocr'})
        assert artifact.status_code == 200, artifact.text
        artifact = artifact.json()
        assert artifact['extracted_text'] == manuscript and artifact['details']['quality']['passed']
        DocumentMonitoringService(self.db).record_revision(self.sid, artifact['artifact_id'], prefix+'-signature', prefix+'-revision')
        self.say(client, '作文写完了，帮我看看', prefix+'-look', 'writing.read_original', target)
        reading = self.say(client, '这页只有我写的答案', prefix+'-scope', 'writing.read_original', target)
        assert reading['action']['type'] == 'read_writing_original', reading
        receipt = readback_receipt(client, self.sid, reading, playback_id=prefix+'-read')
        read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome", json={
            'final_response': '原稿已读回', 'tool_status': 'completed', 'result': receipt})
        assert read.status_code == 200, read.text
        confirm = self.say(client, '对，是我写的，识别没有错', prefix+'-confirm', 'writing.confirm_original', target)
        submitted = client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
        assert submitted.status_code == 200, submitted.text
        follow = submitted.json()['turn']['local_evaluation']
        self.last_confirmation = {'request': confirm, 'execution': submitted.json()}
        assert follow['status'] == 'feedback_required', follow
        evaluated = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert evaluated.status_code == 200, evaluated.text
        assert json.loads(self.gateway.evaluations[-1].messages[1]['content'])['response'] == manuscript
        return follow['feedback_turn_id'], evaluated.json()

    def recovered(self, client, turn):
        response = client.get(f'/v1/agent-turns/{turn}/execution-status')
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed', response.text
        return response.json()['execution']


def assert_specific_feedback(execution):
    speech = execution['execution']['response_text']
    assert speech.count('Make the meeting time easier to find.') == 1, execution['execution']['result'].get('feedback_reason')
    assert 'Revise the meeting time sentence.' not in speech
    assert execution['execution']['result']['certification_status'] == 'candidate'


def assert_rejected_execution(response, fault):
    if fault == 'child':
        assert response.status_code == 200, response.text
        packet = response.json()
        assert packet['execution']['result']['status'] == 'blocked'
        assert packet['execution']['result']['reason'] == 'writing_session_not_active'
        assert packet['execution']['result']['action'] is None
        assert packet['execution']['evidence'] == []
        assert packet['turn']['action']['execution_blocked'] is True
    else:
        assert response.status_code == 409, response.text


def test_actual_paper_feedback_query_restart_original_revision_and_second_manuscript(environment, monkeypatch):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        first_text = synthetic_page(monkeypatch)
        first_turn, first = journey.submit(client, journey.prompt, 'first', *first_text)
        assert_specific_feedback(first)
        assert_specific_feedback(journey.recovered(client, first_turn))
        confirmation = journey.last_confirmation
        query = journey.say(client, '今天计划做到哪了', 'neutral-query', 'planning.describe', None)
        assert query.get('runtime_capability') == 'planning.describe', query
        executed = client.post(f"/v1/agent-turns/{query['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        recovered = journey.recovered(client, first_turn)
        assert_specific_feedback(recovered)
        if os.environ.get('WRITING_CORE_RECEIPT_ARTIFACT'):
            workspace = client.get(f'/v1/sessions/{journey.sid}')
            assert workspace.status_code == 200, workspace.text
            Path(os.environ['WRITING_CORE_RECEIPT_ARTIFACT']).write_text(json.dumps({
                'confirmation': confirmation, 'evaluated': first, 'recovered': recovered,
                'workspace': workspace.json()}, ensure_ascii=False), encoding='utf-8')
        chosen = journey.say(client, '我想按刚才的反馈自己改一遍', 'original-revision', 'writing.revise_original', journey.prompt)
        assert chosen.get('runtime_capability') == 'writing.revise_original', chosen
        binding = chosen['action']['writing_followup_binding']['action']
        assert binding['parent_prompt_id'] == journey.prompt['prompt_id']
        assert binding['assessment_id'] == first['execution']['result']['rubric_assessment']['assessment_id']
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code == 200, prepared.text
        target = prepared.json()['execution']['result']['prompt']
        assert target['prompt_id'] != journey.prompt['prompt_id']
        second_turn, second = journey.submit(client, target, 'second', *revised_page(monkeypatch, 'revision'))
        assert_specific_feedback(second)
        assert second_turn != first_turn and len(journey.gateway.evaluations) == 2
        replay = client.post(f'/v1/agent-turns/{second_turn}/execute')
        assert replay.status_code == 200, replay.text
        assert len(journey.gateway.evaluations) == 2
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['cancel', 'new_prompt', 'child', 'privacy'])
def test_actual_paper_focus_does_not_revive_cancelled_changed_or_foreign_work(environment, monkeypatch, fault):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        first_turn, _ = journey.submit(client, journey.prompt, 'first', *synthetic_page(monkeypatch))
        chosen = journey.say(client, '我想按刚才的反馈自己改一遍', 'before-change', 'writing.revise_original', journey.prompt)
        assert chosen.get('runtime_capability') == 'writing.revise_original', chosen
        if fault == 'cancel':
            stopped = client.post(f'/v1/sessions/{journey.sid}/agent-turn', json={
                'transcript': '别讲了', 'client_action_id': 'cancel-core-writing', 'cloud_text_allowed': False})
            assert stopped.status_code == 200, stopped.text
        elif fault == 'new_prompt':
            from test_diagnostic_prompt_http import prepare
            prepared = prepare(client, journey.sid, track='pet', preferred_item_id='diag-pet-writing-2', client_action_id='different-question')
            assert prepared['prompt']['prompt_id'] != journey.prompt['prompt_id']
        elif fault == 'child':
            switched = client.post(f'/v1/sessions/{journey.sid}/switch-child', json={
                'target_child_id': OTHER, 'client_action_id': 'switch-core-writing'})
            assert switched.status_code == 200, switched.text
        else:
            revoked = client.put('/v1/parent/privacy', headers=journey.headers, json={'cloud_model_text_allowed': False})
            assert revoked.status_code == 200, revoked.text
        # Disabling cloud text is not a cancellation of every local command.
        # Here it must suppress stale model feedback; explicit cancellation,
        # changed target and changed child separately reject late execution.
        if fault != 'privacy':
            late = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
            assert_rejected_execution(late, fault)
        recovered = journey.recovered(client, first_turn)
        assert recovered['execution']['result']['feedback_status'] == 'blocked'
        assert 'Make the meeting time easier to find.' not in recovered['execution']['response_text']
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        if fault != 'privacy':
            repeated = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
            assert_rejected_execution(repeated, fault)
        else:
            assert journey.recovered(client, first_turn)['execution']['result']['feedback_status'] == 'blocked'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
        assert len(journey.gateway.evaluations) == 1

"""Seven authored oral families, actual local HTTP, synthetic PCM only."""
import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.pet_audio_rubric import dimensions
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_oral_teaching import project
from test_pet_media_evidence import local_review, wav_file
import test_pet_oral_supply_http as supply_fixture
from test_pet_oral_supply_http import setup, post, BASE, CHILD, FAMILIES
from test_voice_runtime_binding import capture, interpret, submit


POINTS = {
    'topic_response': ['说出你想去的地方', '说明想去那里的理由'],
    'picture_description': ['描述图中两个人正在做什么', '说出他们活动的一个不同点'],
    'read_aloud': ['按原文读出故事的开头、经过和结尾'],
    'interaction': ['提出一个参观地点和出行方式', '回应建议并问一个问题'],
    'extended_turn': ['说出想尝试的爱好', '说明理由和第一步怎么做'],
    'daily_dialogue': ['回应去图书馆的邀请', '建议时间并问一个问题'],
    'retell': ['说出故事中的人物和遇到的问题', '讲清楚问题最后怎样解决'],
}
ANSWERS = {
    'topic_response': 'I want to visit the garden because it is quiet. I would walk there with my family and look at the flowers.',
    'picture_description': 'One person is reading a book and another person is writing. They are doing different activities in the study room.',
    'interaction': 'We could visit the school garden by bus. That is a good suggestion. Would you like to go there on Tuesday?',
    'extended_turn': 'I want to learn painting because I enjoy making pictures. First I will get some paper and practise drawing a tree.',
    'daily_dialogue': 'Yes, let us visit the school library tomorrow. We could meet after school because we will have time. Would four oclock be good?',
    'retell': 'Ada found a dry plant in the school garden. She asked her teacher for help. They watered the plant and moved it into the shade.',
}


def original_answer(client, db, settings, monkeypatch, sid, prompt, answer):
    pcm = settings.data_dir / 'synthetic-oral-family.wav'
    wav_file(pcm)
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav',
        lambda *a, **k: (answer, 1000, 'synthetic-local-recognizer', False))
    voice = post(client, f'/v1/sessions/{sid}/asr', dict(wav_base64=base64.b64encode(pcm.read_bytes()).decode(),
        purpose='pet_speaking', client_action_id='family-original', capture_reference={'child_id': CHILD,
            'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}}))
    assert voice['capture_binding']['raw_audio_available']
    heard = interpret(client, sid, answer, 'family-answer', voice['voice_input_id'])
    if heard['intent'] == 'pending_answer_clarification':
        post(client, f"/v1/agent-turns/{heard['agent_turn_id']}/execute", {})
        confirmation = '刚才那句就是我的答案'
        confirmed = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'family-confirm-audio')
        heard = interpret(client, sid, confirmation, 'family-confirm', confirmed)
    assert heard['action']['source_voice_input_id'] == voice['voice_input_id']
    saved = submit(client, sid, prompt, heard, answer=answer, key='family-submit')
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'answered'
    assessment = local_review((db, None, sid, prompt, pcm, None), prompt, answer,
                              approve=False, scored=saved.json())
    return assessment


@pytest.mark.parametrize('family', FAMILIES)
def test_real_family_task_points_audio_dimensions_and_other_prompt_focus(tmp_path, monkeypatch, family):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'F' * 32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    gateway = Gateway({})
    gateway.configured = False
    local = {'X-HSC-Session': settings.session_token}
    packet = supply_fixture.packet
    # Author real task-specific criteria via the normal source publication API.
    monkeypatch.setattr(supply_fixture, 'packet', lambda database, kind: {**packet(database, kind), 'task_points': POINTS[kind]})
    with TestClient(create_app(settings, model_gateway=gateway), headers=local,
                    client=('127.0.0.1', 41930)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True,
            'cloud_model_text_allowed': False, 'cloud_audio_allowed': False,
            'model_improvement_allowed': False}).status_code == 200
        original, source, draft = supply_fixture.authored(client, db, family)
        published = draft
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review':
                body.update(decision='approve', note='Synthetic complete original task, not learner evidence')
            published = post(client, BASE + '/' + draft['candidate_id'] + '/' + operation, body)
        assert published['eligible']
        sid = start(client)
        prompt = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare', dict(track='pet',
            preferred_item_id=published['diagnostic_item_id'], client_action_id='family-ask'))['prompt']
        answer = original['stimulus_text'] if family == 'read_aloud' else ANSWERS[family]
        assessment = original_answer(client, db, settings, monkeypatch, sid, prompt, answer)
        pending = project(db, sid, prompt)
        invitation = '你想再读一次吗？' if family == 'read_aloud' else '你想补充一句吗？'
        assert pending['spoken_feedback'] == '我们核对一下：' + POINTS[family][0] + '。' + invitation
        assert pending['feedback'] == [] and not pending['skill_scoring_allowed']
        assert pending['task_points'] == [dict(task_point=p, status='not_assessed') for p in POINTS[family]]
        path = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}"
        text_body = dict(expected_revision=0, client_action_id='text-only', operation='approve',
            reason='Text alone cannot assess pronunciation', independent=True, hint_level=0,
            dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[answer],
                rationale='Synthetic text content reference') for d in assessment['dimensions']],
            criterion_evidence=[dict(criterion=v, response_quote=answer) for v in assessment['criteria']])
        post(client, path + '/review', text_body)
        text_only = project(db, sid, prompt)
        assert text_only['feedback'] == [] and not text_only['skill_scoring_allowed']
        detail = client.get(path).json()
        binding = detail['audio_review']['bindings'][0]
        audio = client.get(path + '/audio', params={'media_id': binding['media_id'], 'audio_sha256': binding['audio']['sha256']})
        assert audio.status_code == 200 and audio.headers['cache-control'] == 'no-store'
        advice = '这一次可以把前后两句话接起来，再完整说一遍。' if family != 'read_aloud' else '这一次可以在句号处稍作停顿，再读一遍。'
        reviewed = {**text_body, 'expected_revision': 1, 'client_action_id': 'audio-specific',
            'review_profile': 'pet_speaking_audio_v1',
            'dimensions': [dict(dimension_id=d['dimension_id'], score=0,
                response_quotes=[answer] if d['dimension_id'] in {'task', 'coherence', 'language'} else [],
                rationale=advice) for d in detail['audio_review']['dimensions']],
            'audio_evidence': {'media_id': binding['media_id'], 'review': {
                'schema': 'pet_speaking_audio_references_v1', 'binding_version': binding['binding_version'],
                'audio_sha256': binding['audio']['sha256'], 'observations': [dict(dimension_id=name,
                    start_frame=0, end_frame=binding['audio']['frames'],
                    heard_text='Synthetic annotation, not real pronunciation evidence',
                    rationale='Synthetic reviewer annotation tests binding, not sound quality')
                    for name in binding['dimensions_required']]}}}
        result = post(client, path + '/review', reviewed)
        view = project(db, sid, prompt)
        assert view['status'] == 'reviewed_audio_feedback' and view['spoken_feedback'] == advice
        assert {f['dimension_id'] for f in view['feedback']} == {d['dimension_id'] for d in dimensions()}
        assert not view['skill_scoring_allowed'] and not view['mastery_claim_allowed']
        assert view['review_ref']['review_revision'] == 2
        # Another real waiting prompt must not replace this answer's feedback focus.
        other = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare', dict(track='pet',
            preferred_item_id='diag-pet-speaking-2', client_action_id='other-prompt'))['prompt']
        assert other['prompt_id'] != prompt['prompt_id']
        assert project(db, sid, prompt) == view
        assert project(db, sid, other)['status'] == 'waiting_answer'
        assert post(client, path + '/review', reviewed)['event'] == result['event']
        assert view['next_action']['binding']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway), headers=local,
                    client=('127.0.0.1', 41931)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        assert project(db, sid, prompt) == view
        assert project(db, sid, other)['status'] == 'waiting_answer'
    assert not gateway.requests

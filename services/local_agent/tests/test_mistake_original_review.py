import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.mistake_learning import MistakeLearningService
from home_study_agent.mistake_learning_http import create_router
from home_study_agent.mistake_learning_intake import MistakeLearningIntake
from home_study_agent.mistake_learning_privacy import export_learning, purge_learning
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.realtime_native_context import claim_mistake_workspace_input
from home_study_agent.repository import StudyRepository
from normal_provider_owner import normal_provider_owner


def test_original_review_uses_only_owned_fresh_ocr_or_matching_voice_and_survives_restart(tmp_path):
    db = Database(tmp_path / 'review.db')
    db.initialize()
    PrivacyLifecycleService(db, tmp_path).initialize()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    with db.connect() as c:
        for child, session, status in [('child-a', 'session-a','active'), ('child-b', 'session-b','completed')]:
            c.execute('''INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                VALUES(?,?,'六年级','江苏南京','active',?,?)''', (child, child, stamp, stamp))
            c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                VALUES(?,?,?,'study',?,?,?)''', (session, child, status, stamp, stamp, session))
        c.execute('''INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status,last_seen_at)
            VALUES('camera','document_camera','usb-camera','{}','verified',?)''', (stamp,))
        c.execute('''INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at)
            VALUES('lease-a','session-a','camera','document_camera','active',?,?,?)''',
            (stamp, stamp, (now + timedelta(minutes=5)).isoformat()))
        c.execute('''INSERT INTO voice_input(voice_input_id,child_id,session_id,purpose,transcript,model_version,status,created_at,client_action_id)
            VALUES('voice-a','child-a','session-a','command','我把除数取倒数，最终答案是八分之十五。','local-asr','completed',?,'voice-a')''',
            (stamp,))
        c.execute('''INSERT INTO voice_capture_binding(client_action_id,session_id,child_id,wav_sha256,purpose,status,
            voice_input_id,source_kind,created_at,updated_at)
            VALUES('voice-a','session-a','child-a','fixture-digest','agent_command','completed','voice-a','local_wav',?,?)''',
            (stamp, stamp))
        c.commit()
    intake = MistakeLearningIntake(db)
    question = '12. 计算 3/4 ÷ 2/5，并写出最简结果。'
    card = intake.create(child_id='child-a', session_id='session-a', question=question, client_action_id='source-a')
    other = intake.create(child_id='child-b', question=question, client_action_id='source-b')
    mid = card['mistake_id']
    same_child_other = intake.create(child_id='child-a',question='计算 3+4。',client_action_id='second-source-a')

    class CapturedOcr:
        def process(self, session_id, image, action_id, store_raw):
            assert session_id == 'session-a' and store_raw is False
            artifact = action_id + '-artifact'
            confidence = .91 if image.startswith(b'sharp') else .4
            details = json.dumps({'local_pipeline': {'image_sha256': hashlib.sha256(image).hexdigest()}})
            with db.connect() as c:
                c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
                    extracted_text,confidence,details_json,status,created_at,client_action_id)
                    VALUES(?,'child-a','session-a','document_ocr',NULL,'3/4 ÷ 2/5 = 3/10',?,?,'completed',?,?)''',
                    (artifact, confidence, details, stamp, action_id))
                c.commit()
            return {'artifact_id': artifact}

    def application():
        app = FastAPI()
        app.include_router(create_router(MistakeLearningService(db, None),
            authorize_request=lambda req: req.headers.get('X-Session') == 'test', ocr_workflow=CapturedOcr()))
        return TestClient(app, headers={'X-Session': 'test'})

    path = f'/v1/mistakes/{mid}/learning'
    ocr = '/v1/sessions/session-a/mistake-review/ocr'
    with application() as client:
        initial = client.get(path, params={'child_id':'child-a', 'session_id':'session-a'}).json()
        assert initial['source']['created_at'] and initial['source']['has_question_crop'] is False
        payload = {'child_id':'child-a','session_id':'session-a','source_revision':initial['source_revision']}
        def frame(capture_id, image):
            return {'child_id':'child-a','lease_id':'lease-a','client_action_id':capture_id,
                    'image_base64':base64.b64encode(image).decode()}
        unreadable_frame = frame('unclear-ocr', b'blur' * 12)
        assert client.post(ocr, json=unreadable_frame | {'lease_id':'bad-lease'}).status_code == 403
        unreadable = client.post(ocr, json=unreadable_frame).json()['artifact_id']
        uncertain = client.post(path+'/review', json=payload | {'client_action_id':'unclear-review','modality':'paper','artifact_id':unreadable}).json()
        assert uncertain['review_result']['judgement'] == 'uncertain'
        assert uncertain['original_review']['wrong_count'] == 0

        sharp_frame = frame('sharp-ocr', b'sharp' * 12)
        artifact = client.post(ocr, json=sharp_frame).json()['artifact_id']
        assert client.post(ocr, json=sharp_frame).json() == {'artifact_id':artifact}
        assert client.post(ocr, json=sharp_frame | {'image_base64':unreadable_frame['image_base64']}).status_code == 409
        paper = payload | {'client_action_id':'wrong-paper','modality':'paper','artifact_id':artifact,'utterance':'15/8'}
        wrong = client.post(path+'/review', json=paper).json()
        assert wrong['review_result']['judgement'] == 'incorrect'
        assert wrong['original_review']['wrong_count'] == 1
        assert wrong['turns'][-1]['text'] == wrong['review_result']['next_hint']
        assert client.post(path+'/review', json=paper).json() == wrong
        assert client.post(path+'/review', json=paper | {'client_action_id':'duplicate-frame'}).status_code == 409
        assert client.post(f"/v1/mistakes/{other['mistake_id']}/learning/review", json=paper | {
            'child_id':'child-b','client_action_id':'other-review'}).status_code == 409
        assert client.post(path+'/review', json=payload | {'client_action_id':'wrong-voice','modality':'voice','utterance':'我的答案是三分之十'}).json()['original_review']['wrong_count'] == 2
        spoken = payload | {'client_action_id':'correct-voice','modality':'voice','voice_input_id':'voice-a',
                            'utterance':'我把除数取倒数，最终答案是八分之十五。'}
        assert client.post(path+'/review', json=spoken).status_code == 409
        claim = {'child_id':'child-a','voice_input_id':'voice-a','kind':'answer',
                 'after_ms':int(now.timestamp()*1000)-1,'mistake_id':mid,'source_revision':initial['source_revision']}
        assert claim_mistake_workspace_input(db,'session-a',claim | {'after_ms':int(now.timestamp()*1000)+1}) == {'claimed':False,'stale':True}
        assert claim_mistake_workspace_input(db,'session-a',claim | {'source_revision':'0'*64}) == {'claimed':False,'stale':True}
        assert claim_mistake_workspace_input(db,'session-a',claim) == {'claimed':True}
        assert claim_mistake_workspace_input(db,'session-a',claim) == {'claimed':False,'already_handled':True}
        other_path = f"/v1/mistakes/{same_child_other['mistake_id']}/learning"
        other_revision = client.get(other_path,params={'child_id':'child-a'}).json()['source_revision']
        assert client.post(other_path+'/review',json=spoken | {'source_revision':other_revision}).status_code == 409
        assert client.post(path+'/review', json=spoken | {'utterance':'结果是八分之十五'}).status_code == 409
        correct = client.post(path+'/review', json=spoken).json()
        assert correct['original_review']['status'] == 'retest_pending'
        assert correct['original_review']['wrong_count'] == 2
        assert not correct['review_due']

    with application() as client:
        reopened = client.get(path, params={'child_id':'child-a'}).json()
        assert reopened['original_review'] == correct['original_review']
        assert client.get(path+'/actions/correct-voice', params={'child_id':'child-a'}).json()['review_result'] == correct['review_result']
    with db.connect() as c:
        persisted = export_learning(c,'child-a')['mistake_learning_original_review']
        assert [row['judgement'] for row in sorted(persisted, key=lambda row: row['sequence'])] == ['incorrect','incorrect','correct']
        counts = purge_learning(c,'child-a')
        assert counts['mistake_learning_original_review'] == 3 and counts['mistake_review_ocr'] == 2
        assert export_learning(c,'child-a')['mistake_learning_original_review'] == []


@pytest.fixture
def cloud_review(tmp_path):
    db = Database(tmp_path / 'original-cloud.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    child = 'child-junior-3'
    session = repo.start_session(child, 'review-session')['session_id']
    question = '请解释植物进行光合作用时为什么需要光照，并指出光能转化成了什么形式的能量。'
    intake = MistakeLearningIntake(db)
    mid = intake.create(child_id=child, question=question, session_id=session, client_action_id='public-biology')['mistake_id']

    class Gateway:
        def __init__(self):
            self.calls = []
            self.mode = 'correct'

        async def complete(self, request):
            request.pre_dispatch()
            context = json.loads(request.messages[-1]['content'])
            stage = request.context_manifest['stage']
            self.calls.append((stage, request.request_type, context))
            if stage == 'rubric':
                value = {'status':'ready', 'task_points':['光提供能量', '光能转化为化学能']}
            else:
                missed = self.mode in ('wrong', 'shared_contradiction') or self.mode == 'disagree' and stage == 'verification'
                first_status = 'partial' if self.mode == 'shared_contradiction' and stage == 'verification' else 'supported'
                second_status = 'contradicted' if self.mode == 'shared_contradiction' else 'not_addressed' if missed else 'supported'
                evidence = '不存在于回答的短句' if self.mode == 'bad_quote' else '储存在糖里的化学能'
                if second_status == 'contradicted':
                    evidence = '光能全变成热能'
                value = {
                    'version':'formative_criterion_review_v1', 'confidence':.94, 'rubric_dimensions':[],
                    'criterion_checks':[
                        {'criterion_index':0, 'status':first_status, 'response_quotes':['光给反应提供能量'], 'rationale':'说出了光的作用。'},
                        {'criterion_index':1, 'status':second_status,
                         'response_quotes':[] if second_status == 'not_addressed' else [evidence], 'rationale':'核对能量的去向。'},
                    ],
                    'feedback':{'acknowledgement':'你说明了光对反应的作用。', 'focus_index':1 if missed else None,
                                'question':'这些能量最后存在哪里？' if missed else None},
                    'next_check':'换到另一种植物解释能量去向。',
                }
            if self.mode == 'revoked' and stage == 'assessment':
                privacy.update_preferences({'cloud_model_text_allowed': False})
            return SimpleNamespace(content=json.dumps(value), provider='deepseek', model='configured-grader', latency_ms=80,
                                   prompt_tokens=123, completion_tokens=57)

    gateway = Gateway()
    service = MistakeLearningService(db, gateway)
    app = FastAPI()
    app.include_router(create_router(service, authorize_request=lambda _: True))
    with TestClient(app) as client:
        yield db, privacy, gateway, client, child, session, mid


def test_open_original_reviews_use_scoped_cloud_grading_and_persist(cloud_review):
    db, privacy, gateway, client, child, session, mid = cloud_review
    path = f'/v1/mistakes/{mid}/learning'
    source_revision = client.get(path, params={'child_id':child}).json()['source_revision']
    body = {'child_id':child, 'session_id':session, 'source_revision':source_revision, 'modality':'voice',
            'client_action_id':'public-cloud-wrong', 'utterance':'光给反应提供能量，让光合作用继续进行。'}
    gateway.mode = 'wrong'
    wrong = client.post(path+'/review', json=body).json()
    assert wrong['review_result']['judgement'] == 'incorrect'
    assert wrong['review_result']['next_hint'] == '这些能量最后存在哪里？'
    assert wrong['original_review']['wrong_count'] == 1
    assert wrong['turns'][-1]['text'] == wrong['review_result']['next_hint']
    assert client.post(path+'/review', json=body).json() == wrong
    assert [call[0] for call in gateway.calls] == ['rubric','assessment','verification']
    assert [call[1] for call in gateway.calls] == ['paper_scoring_authority_draft','learning_outcome_evaluation','learning_outcome_evaluation']
    assert set(gateway.calls[0][2]) == {'question','reference_groups'}
    assert gateway.calls[1][2] == gateway.calls[2][2]
    gateway.mode = 'correct'
    body |= {'client_action_id':'public-cloud-correct', 'utterance':'光给反应提供能量，最终转化成储存在糖里的化学能。邮箱：test@example.test'}
    correct = client.post(path+'/review', json=body).json()
    assert correct['review_result']['judgement'] == 'correct'
    assert correct['review_result']['grading_basis'] == 'cloud_formative'
    assert correct['original_review']['status'] == 'retest_pending'
    assert correct['original_review']['wrong_count'] == 1
    assert 'test@example.test' not in json.dumps(gateway.calls, ensure_ascii=False)
    assert MistakeLearningService(db, None).state(mid, child_id=child)['original_review'] == correct['original_review']
    with db.connect() as c:
        row = c.execute('SELECT provider_json FROM mistake_learning_action WHERE client_action_id=?', (body['client_action_id'],)).fetchone()
        evidence = json.loads(row[0])
        assert evidence['calls']['verification']['model'] == 'configured-grader'
        assert evidence['evidence'][1]['response_quotes'] == ['储存在糖里的化学能']


def test_open_review_counts_only_shared_grounded_error(cloud_review):
    db, _, gateway, client, child, session, mid = cloud_review
    gateway.mode = 'shared_contradiction'
    path = f'/v1/mistakes/{mid}/learning/review'
    action = 'public-cloud-agreed-error'
    answer = client.post(path, json={
        'child_id': child, 'session_id': session, 'modality': 'voice', 'client_action_id': action,
        'utterance': '光给反应提供能量，让光合作用继续进行。光能全变成热能。'
    }).json()
    assert answer['review_result']['judgement'] == 'incorrect'
    assert answer['original_review']['wrong_count'] == 1
    with db.connect() as c:
        row = c.execute('SELECT provider_json FROM mistake_learning_action WHERE client_action_id=?', (action,)).fetchone()
    assert json.loads(row[0])['evidence'] == [{
        'criterion_index': 1, 'status': 'contradicted', 'response_quotes': ['光能全变成热能']
    }]


def test_open_review_does_not_send_protected_local_reference(cloud_review, monkeypatch):
    db, _, gateway, client, child, session, mid = cloud_review
    path = f'/v1/mistakes/{mid}/learning'
    question = client.get(path, params={'child_id':child}).json()['source']['question']
    monkeypatch.setattr(PaperScoringService, '_local_source_current', staticmethod(lambda *_: True))
    PaperScoringService(db).register_exact_authority(child_id=child, session_id=None, question_text=question,
        expected_answers=['由本机教材保存的光合作用参考句'], authority_type='published_answer_key',
        authority_ref='local-answer-source:public-fixture:v1', rubric_version='v1', client_action_id='public-answer-key')
    reply = client.post(path+'/review', json={'child_id':child, 'session_id':session, 'modality':'voice',
        'client_action_id':'public-review-with-protected-reference', 'utterance':'光给反应提供能量，最终转化成储存在糖里的化学能。'})
    assert reply.json()['review_result']['judgement'] == 'correct'
    assert gateway.calls[0][2]['reference_groups'] == []
    assert '由本机教材保存的' not in json.dumps(gateway.calls, ensure_ascii=False)


def test_open_review_routes_through_general_json_endpoint(cloud_review, normal_provider_owner, tmp_path):
    db, _, fixture, _, child, session, mid = cloud_review
    calls = []

    async def transport(request):
        payload = json.loads(request.content)
        calls.append((request.url.host, payload['model'], payload['response_format']))
        stage = ('rubric', 'assessment', 'verification')[len(calls)-1]
        candidate = await fixture.complete(SimpleNamespace(pre_dispatch=lambda: None,
            messages=payload['messages'], context_manifest={'stage':stage}, request_type='fixture'))
        return httpx.Response(200, request=request, json={
            'choices':[{'finish_reason':'stop','message':{'content':candidate.content}}],
            'usage':{'prompt_tokens':11,'completion_tokens':9},
        })

    settings = Settings(data_dir=tmp_path, database_path=db.path, model_api_key='synthetic-grader-key',
        model_base_url='https://grader.example.test/v1', model_name='synthetic-grader',
        tutor_explanation_api_key='synthetic-explanation-key')
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))

    async def review():
        try:
            return await MistakeLearningService(db, gateway).review_with_cloud(mid, child_id=child,
                session_id=session, modality='voice', client_action_id='public-review-routing',
                utterance='光给反应提供能量，最终转化成储存在糖里的化学能。')
        finally:
            await gateway.close()

    result = asyncio.run(review())
    assert result['review_result']['grading_basis'] == 'cloud_formative'
    assert calls == [('grader.example.test','synthetic-grader',{'type':'json_object'})] * 3


@pytest.mark.parametrize('mode', ['disagree','bad_quote','revoked','no_consent'])
def test_inconclusive_cloud_review_does_not_count_as_wrong(cloud_review, mode):
    db, privacy, gateway, client, child, session, mid = cloud_review
    gateway.mode = mode
    if mode == 'no_consent':
        privacy.update_preferences({'cloud_model_text_allowed': False})
    path = f'/v1/mistakes/{mid}/learning'
    body = {'child_id':child, 'session_id':session, 'modality':'voice', 'client_action_id':'public-cloud-inconclusive',
            'utterance':'光给反应提供能量，最终转化成储存在糖里的化学能。'}
    reply = client.post(path+'/review', json=body)
    if mode == 'revoked':
        assert reply.status_code == 403
        assert len(gateway.calls) == 2
    else:
        assert reply.status_code == 200
        assert reply.json()['review_result']['judgement'] == 'uncertain'
        if mode == 'no_consent':
            assert gateway.calls == []
    assert client.get(path, params={'child_id':child}).json()['original_review']['wrong_count'] == 0

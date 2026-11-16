"""Real generated listening tasks must wait for original audio, not merely a timer."""
import pytest
import json
import httpx
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from test_pet_task_execution import CHILD, command, generated, published_listening_case
from pet_listening_fixture import audio_artifact
from home_study_agent.database import Database
from home_study_agent.pet_listening_admission import resolve_pet_listening_resource
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_voice_runtime_binding import capture, interpret


def bind_audio(client, db, root, sid, prompt, *, approved=True, key='original-audio', full_reference=True):
    aid, path = audio_artifact(db, root, prompt, key=key)
    base = f'/v1/sessions/{sid}/pet-media'
    result = client.post(base + '/listening/begin', json={'prompt_id': prompt['prompt_id'],
        'item_version': prompt['item_version'], 'artifact_id': aid, 'client_action_id': key + ':bind'})
    assert result.status_code == 200, result.text
    media = result.json()['media']
    if approved:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        reviewed = client.post(base + f"/{media['media_id']}/source-review", headers={'X-HSC-Parent': token},
            json={'audio_sha256': media['audio']['sha256'], 'operation': 'approve', 'full_spoken_reference': full_reference, 'client_action_id': key + ':approve'})
        assert reviewed.status_code == 200, reviewed.text
        media = reviewed.json()['media']
    return media, path


def pending(client, case):
    tid = command(case, child_id=CHILD)
    result = client.post(f'/v1/agent-turns/{tid}/execute')
    assert result.status_code == 200, result.text
    assert result.json()['execution']['result']['status'] == 'awaiting_resources'
    prompt = result.json()['execution']['result']['diagnostic_prompt']['prompt']
    assert client.post(f'/v1/agent-turns/{tid}/execute').json()['execution'] == result.json()['execution']
    return tid, prompt


@pytest.mark.parametrize('published', [False, True])
def test_generated_listening_without_original_audio_keeps_prompt_but_not_timer(tmp_path, published):
    case = published_listening_case(tmp_path)[0] if published else generated(tmp_path, 'listening')
    db, sid, pid, _, original = case
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        turn = command(case, child_id=CHILD)
        result = client.post(f'/v1/agent-turns/{turn}/execute')
        assert result.status_code == 200, result.text
        prompt = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()
        assert prompt['prompt']['diagnostic_item_id'] == original['diagnostic_item_id']
        media = PetMediaEvidenceService(db, media_root=tmp_path).list_for_prompt(sid,
            prompt_id=prompt['prompt']['prompt_id'], item_version=prompt['prompt']['item_version'])
        assert media['status'] == 'missing_media' and media['listening'] == []
        with db.connect() as c:
            item = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()
            assert item['status'] == 'pending' and item['started_at'] is None, dict(item)
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type IN ('started','resumed')", (pid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=? AND diagnostic_item_id=?',
                             (sid, original['diagnostic_item_id'])).fetchone()[0] == 0
        assert prompt['status'] == 'awaiting_task_start'


@pytest.mark.parametrize('fault,reason', [('unreviewed','listening_material_review_required'),
    ('ambiguous','listening_material_ambiguous'), ('badfile','listening_material_invalid'),
    ('wrongowner','listening_material_invalid'), ('revoked','listening_material_review_required'),
    ('requirement','listening_requirement_unverified'), ('no_runtime','listening_media_runtime_unavailable')])
def test_readiness_uses_current_real_resource_without_scoring_or_writes(tmp_path, fault, reason):
    case = generated(tmp_path, 'listening')
    db, sid, pid, _, _ = case
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        client.post('/v1/parent/setup', json={'pin':'2468'})
        _, prompt = pending(client, case)
        media, path = bind_audio(client, db, tmp_path, sid, prompt, approved=fault != 'unreviewed')
        if fault == 'ambiguous':
            bind_audio(client, db, tmp_path, sid, prompt, key='other-audio')
        elif fault == 'badfile':
            path.unlink()
        elif fault == 'revoked':
            token = client.post('/v1/parent/unlock', json={'pin':'2468'}).json()['parent_token']
            revoked = client.post(f"/v1/sessions/{sid}/pet-media/{media['media_id']}/source-review", headers={'X-HSC-Parent':token},
                json={'audio_sha256':media['audio']['sha256'],'operation':'retract','client_action_id':'retract-source'})
            assert revoked.status_code == 200, revoked.text
        elif fault in {'wrongowner', 'requirement'}:
            with db.connect() as c:
                if fault == 'wrongowner':
                    c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id=?", (media['artifact_id'],))
                else:
                    c.execute("UPDATE pet_media_binding SET state_json=json_remove(state_json,'$.delivery_requirement') WHERE media_id=?", (media['media_id'],))
                c.commit()
        scoped = Database(db.path, media_root=tmp_path) if fault != 'no_runtime' else db
        with scoped.connect() as c:
            c.execute('BEGIN')
            before = c.total_changes
            value = resolve_pet_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
            assert value['status'] == 'awaiting_resources' and value['reason'] == reason, value
            assert c.total_changes == before and c.in_transaction
            assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0] is None
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if fault != 'no_runtime':
            retry_turn = command(case, child_id=CHILD)
            blocked = client.post(f'/v1/agent-turns/{retry_turn}/execute')
            assert blocked.status_code == 200, blocked.text
            assert blocked.json()['execution']['result']['status'] == 'awaiting_resources'
            assert blocked.json()['execution']['result']['reason'] == reason
            with db.connect() as c:
                assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] is None


@pytest.mark.parametrize('published', [False, True])
def test_actual_parent_audio_review_then_natural_resume_starts_original_after_restart(tmp_path, monkeypatch, published):
    case = published_listening_case(tmp_path)[0] if published else generated(tmp_path, 'listening')
    db, sid, pid, _, _ = case
    PrivacyLifecycleService(db, tmp_path).initialize()
    PrivacyLifecycleService(db, tmp_path).update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec', model_name='isolated-model', voice_intent_model_name='isolated-model')
    requests = []
    def transport(request):
        body = json.loads(request.content)
        data = json.loads(body['messages'][-1]['content'])
        assert set(data) == {'transcript','state'} and data['state']['has_pending_task_start']
        requests.append(body)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'role':'control',
            'control':'task.resume','quote':data['transcript'],'confidence':.99,
            'selector':{'subject':None,'task_kind':None,'relative_order':None}})}}]})
    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app()) as client:
        client.post('/v1/parent/setup',json={'pin':'2468'})
        original_turn, prompt = pending(client, case)
        quote = '继续原任务'
        early_voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'still-missing-audio')
        early_turn = interpret(client, sid, quote, 'still-missing-original', early_voice)
        early_url = f"/v1/agent-turns/{early_turn['agent_turn_id']}/execute"
        early = client.post(early_url)
        assert early.status_code == 200, early.text
        early_result = early.json()['execution']
        assert early_result['result']['status'] == 'awaiting_resources'
        repeated = client.post(early_url)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['execution'] == early_result
        media, _ = bind_audio(client, db, tmp_path, sid, prompt)
        assert media['source_reviewed'] and not media['qualification']['task_delivery_qualified']
        assert not media['qualification']['skill_qualified']
        assert client.post(early_url).json()['execution'] == early_result
    with TestClient(app()) as client:
        assert client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()['status'] == 'awaiting_task_start'
        assert client.post(early_url).json()['execution'] == early_result
        old = client.post(f'/v1/agent-turns/{original_turn}/execute')
        assert old.json()['execution']['result']['status'] == 'awaiting_resources'
        quote = '继续原任务'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'resume-listening-audio')
        heard = interpret(client, sid, quote, 'resume-listening-original', voice)
        assert heard['runtime_capability'] == 'task.resume', heard
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
        value = result.json()['execution']['result']
        assert value['status'] == 'active' and value['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        receipt = client.post(url).json()['execution']
        assert receipt['result']['replayed'] is True
        assert receipt['result']['recorded_receipt']['status'] == 'started'
        assert receipt['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback' ").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?', (prompt['diagnostic_item_id'],)).fetchone()[0] == 0
        assert len(requests) == 2
    with TestClient(app()) as client:
        replay = client.post(url).json()['execution']['result']
        assert replay['recorded_receipt'] == receipt['result']['recorded_receipt']
        assert replay['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        assert len(requests) == 2


def test_review_withdrawn_after_executor_readiness_is_rechecked_inside_timer_transaction(tmp_path, monkeypatch):
    from home_study_agent.repository import StudyRepository
    case = generated(tmp_path, 'listening')
    db, sid, pid, _, _ = case
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        client.post('/v1/parent/setup',json={'pin':'2468'})
        _, prompt = pending(client, case)
        media, _ = bind_audio(client, db, tmp_path, sid, prompt)
        next_turn = command(case, child_id=CHILD)
        original = StudyRepository.start_plan_item
        called = []
        def withdraw(self, *args, **kwargs):
            called.append(True)
            PetMediaEvidenceService(db, media_root=tmp_path).review_listening_source(sid, media_id=media['media_id'],
                audio_sha256=media['audio']['sha256'], operation='retract', reviewer_id='isolated-parent',
                reviewer_role='parent', client_action_id='withdraw-between-transactions')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(StudyRepository, 'start_plan_item', withdraw)
        result = client.post(f'/v1/agent-turns/{next_turn}/execute')
        assert called == [True]
        with db.connect() as c:
            assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] is None
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        assert result.status_code == 200, result.text
        assert result.json()['execution']['result']['status'] == 'awaiting_resources'
        assert result.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']

"""Source review/cancel/transaction boundaries after explicit legacy replacement."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_pet_media_evidence import media, CHILD
from test_legacy_listening_review_resume import legacy_plan, request_resume, execute
from test_listening_review_journey import next_day_case
from test_pet_listening_resource_admission import bind_audio


@pytest.mark.parametrize('fault', ['prompt_cancel', 'approval_retract'])
def test_prepared_replacement_is_not_started_by_cancelled_question_or_retracted_audio(media, tmp_path, monkeypatch, fault):
    settings, db, gateway, review, _, _ = next_day_case(media, tmp_path, monkeypatch)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-recovery-day'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        origin = request_resume(client, settings, monkeypatch, sid, 'recovery-original')
        first = execute(client, origin)
        prompt = first['result']['diagnostic_prompt']['prompt']
        pid = first['result']['plan_item_id']
        assert first['result']['status'] == 'awaiting_resources'
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        audio, _ = bind_audio(client, db, tmp_path, sid, prompt, key='recovery-original-audio')
        request = request_resume(client, settings, monkeypatch, sid, 'recovery-ready', prompt)
        if fault == 'prompt_cancel':
            result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel', json={
                'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
        else:
            parent = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
            result = client.post(f"/v1/sessions/{sid}/pet-media/{audio['media_id']}/source-review",
                headers={'X-HSC-Parent': parent}, json={'audio_sha256': audio['audio']['sha256'],
                    'operation': 'retract', 'client_action_id': 'retract-current-original'})
        assert result.status_code == 200, result.text
        stopped = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert stopped.status_code in {200, 409, 422}, stopped.text
        if stopped.status_code == 200:
            assert stopped.json()['execution']['result']['status'] in {'awaiting_resources', 'blocked'}
        assert execute(client, origin) == first
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
        assert read.status_code == 200, read.text
        assert read.json()['status'] == ('cancelled' if fault == 'prompt_cancel' else 'awaiting_task_start')
        with db.connect() as c:
            assert c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[:] == ('pending', None)
            assert c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone()[:] == ('cancelled', None)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1


def test_atomic_replacement_timer_fault_rolls_back_old_item_and_plan_cas(media, tmp_path, monkeypatch):
    from home_study_agent.repository import StudyRepository
    settings, db, gateway, _, _, _ = next_day_case(media, tmp_path, monkeypatch, ready_source=True)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-atomic-day'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        request = request_resume(client, settings, monkeypatch, sid, 'atomic-original')
        with db.connect() as c:
            plan = tuple(c.execute('SELECT version,total_minutes FROM daily_plan WHERE plan_id=?', (old['plan_id'],)).fetchone())
        actual = StudyRepository.start_plan_item
        def fail_after_start(self, *args, **kwargs):
            assert kwargs['connection'].in_transaction
            actual(self, *args, **kwargs)
            raise RuntimeError('isolated timer transaction interruption')
        with monkeypatch.context() as patch:
            patch.setattr(StudyRepository, 'start_plan_item', fail_after_start)
            with pytest.raises(RuntimeError, match='isolated timer transaction interruption'):
                client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone()) == old
            assert tuple(c.execute('SELECT version,total_minutes FROM daily_plan WHERE plan_id=?', (old['plan_id'],)).fetchone()) == plan
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (request['agent_turn_id'],)).fetchone()[0])
            assert 'legacy_listening_review_replacement' not in manifest
        retried = request_resume(client, settings, monkeypatch, sid, 'atomic-new-request')
        result = execute(client, retried)
        assert result['result']['status'] == 'active', result
        replayed = execute(client, retried)
        assert replayed['result']['plan_item_id'] == result['result']['plan_item_id']
        assert replayed['result']['diagnostic_prompt']['prompt']['prompt_id'] == result['result']['diagnostic_prompt']['prompt']['prompt_id']
        assert replayed['result']['recorded_receipt']['status'] == 'started'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event e JOIN plan_item p USING(plan_item_id) WHERE p.plan_id=? AND e.event_type='started'", (old['plan_id'],)).fetchone()[0] == 1

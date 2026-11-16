"""The child's explicit listening selector still resolves the prepared original."""
from dataclasses import replace
import json
import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.task_start_binding import TaskStartBinding
from test_pet_media_evidence import media, CHILD
from test_listening_review_journey import next_day_case
from test_legacy_listening_review_resume import legacy_plan, request_resume, execute


def test_explicit_listening_selector_restores_exact_prepared_review_not_any_english_task(media, tmp_path, monkeypatch):
    settings, db, gateway, _, _, _ = next_day_case(media, tmp_path, monkeypatch)
    actual = gateway.complete
    async def listening(request):
        result = await actual(request)
        if request.request_type == 'voice_interaction':
            decision = json.loads(result.content)
            if decision.get('control') == 'task.resume':
                decision['selector'] = {'subject': 'english', 'task_kind': 'listening', 'relative_order': None}
                result = replace(result, content=json.dumps(decision))
        return result
    gateway.complete = listening
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'listening-selector-day'}).json()['session_id']
        legacy_plan(client, db, monkeypatch, sid)
        first = execute(client, request_resume(client, settings, monkeypatch, sid, 'original-selector'))
        prompt = first['result']['diagnostic_prompt']['prompt']
        assert first['result']['status'] == 'awaiting_resources'
        again = request_resume(client, settings, monkeypatch, sid, 'prepared-selector', prompt)
        result = execute(client, again)
        assert result['result']['status'] == 'awaiting_resources'
        assert result['result']['plan_item_id'] == first['result']['plan_item_id']
        assert result['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    {'review_task_id': 'other-review'}, {'skill': 'reading'}, {'version': 'unknown'},
])
def test_listening_selector_does_not_infer_from_subject_or_review_title(change):
    binding = {'version': 'pet_listening_review_binding_v1', 'review_task_id': 'original-review',
               'skill': 'listening', **change}
    item = {'subject': '英语', 'task_type': 'review', 'source_type': 'due_review',
            'source_ref': 'original-review', 'goal_prompt_binding': {'binding': binding}}
    selected = TaskStartBinding.select_resume_target({'items': [item]},
        {'subject': 'english', 'task_kind': 'listening', 'relative_order': None})
    assert selected['item'] is None

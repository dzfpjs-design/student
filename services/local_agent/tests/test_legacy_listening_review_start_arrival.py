"""A queued direct start cannot switch from its captured legacy review to a new top slot."""
import pytest
import json
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_pet_media_evidence import media, CHILD
from test_listening_review_journey import next_day_case
from test_legacy_listening_review_resume import legacy_plan
from test_legacy_listening_review_guards import direct_start


def test_direct_start_uses_arrival_before_selecting_a_different_current_top(media, tmp_path, monkeypatch):
    settings, db, gateway, _, _, _ = next_day_case(media, tmp_path, monkeypatch)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-race-day'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        request = direct_start(client, settings, db, gateway, monkeypatch, sid, 'before-reorder')
        with db.connect() as c:
            other = c.execute("SELECT * FROM plan_item WHERE plan_id=? AND plan_item_id!=? AND status='pending' ORDER BY position LIMIT 1",
                (old['plan_id'], old['plan_item_id'])).fetchone()
            assert other is not None  # Another real generated slot; no injected task or binding.
            c.execute('UPDATE plan_item SET position=-1 WHERE plan_item_id=?', (other['plan_item_id'],))
            c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=?', (old['plan_id'],))
            before = [tuple(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))]
            c.commit()
        result = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert result.json()['detail']['code'] == 'TASK_START_LEGACY_REVIEW_PLAN_CHANGED'
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))] == before
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
        # A genuinely new request freezes the new plan version but still names
        # the original review, not the other task moved to the top.
        new_request = direct_start(client, settings, db, gateway, monkeypatch, sid, 'after-reorder')
        fresh = client.post(f"/v1/agent-turns/{new_request['agent_turn_id']}/execute")
        assert fresh.status_code == 200, fresh.text
        value = fresh.json()['execution']['result']
        assert value['status'] == 'awaiting_resources'
        assert value['plan_item_id'] not in {old['plan_item_id'], other['plan_item_id']}
        assert value['diagnostic_prompt']['prompt']['review_task_id'] == old['source_ref']
        retry = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert retry.status_code == 409, retry.text
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (other['plan_item_id'],)).fetchone()) == {**dict(other), 'position': -1}
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 1


def test_direct_registered_start_cannot_promote_unknown_semantic_receipt(media, tmp_path, monkeypatch):
    from home_study_agent.model_gateway import ModelResult
    settings, db, gateway, _, _, _ = next_day_case(media, tmp_path, monkeypatch)
    async def unknown(request):
        return ModelResult(content=json.dumps({'role': 'unknown', 'control': None, 'quote': '开始任务', 'confidence': .99}),
            provider='test', model='synthetic-control', latency_ms=1)
    gateway.complete = unknown
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-unknown-day'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        # Even an erroneous caller registering this capability cannot convert a
        # non-control semantic receipt into authority to replace the old task.
        request = direct_start(client, settings, db, gateway, monkeypatch, sid, 'unknown-command', expect_control=False)
        result = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert result.json()['detail']['code'] == 'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED'
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone()) == old
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0

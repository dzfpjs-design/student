"""COPY-only adversarial requests around a real legacy plan, not invented slots."""
import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.task_start_binding import TaskStartBinding
from home_study_agent.voice_control_intent import VoiceControlIntent, STATE_FIELDS
from home_study_agent.learning_input_revision import LearningInputRevisionService
from test_pet_media_evidence import media, CHILD
from test_legacy_listening_review_resume import legacy_plan, request_resume, execute
from test_listening_review_journey import next_day_case
from test_voice_runtime_binding import capture


def direct_start(client, settings, db, gateway, monkeypatch, sid, key, *, expect_control=True):
    """Direct registered Runtime producer, not a natural-language HTTP assertion.

    The plan, item and frozen arrival all come from production code. Only the
    registered capability selection is the fixture; it cannot supply an item ID.
    """
    text = '开始任务'
    voice_id = capture(client, settings, monkeypatch, sid, text, key=key + ':voice')
    arrival = LearningInputRevisionService(db).capture_arrival(sid, key, text)
    with db.connect() as c:
        from home_study_agent.writing_daily_followup import capture_arrival as capture_daily_writing
        c.execute('BEGIN IMMEDIATE')
        capture_daily_writing(c, arrival['arrival_id'])
        targets = TaskStartBinding.resume_snapshot(c, sid, CHILD)
        voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=sid, child_id=CHILD,
            voice_input_id=voice_id, transcript=text)
        c.commit()
    classifier = VoiceControlIntent(db, StudyRepository(db), gateway, settings)
    decision = asyncio.run(classifier.resolve(session_id=sid, child_id=CHILD, action_id=key,
        transcript=text, state={**{k: False for k in STATE_FIELDS}, 'is_study_phase': True, 'has_pending_task_start': True},
        local_sources=[], names=[], allowed=True, binding={'task_resume': targets}, voice_capture=voice))
    assert decision['status'] == ('control' if expect_control else 'non_control'), decision
    runtime = AgentRuntimeStore(db)
    cap = CapabilityRegistry().require('task.start', 'study')
    turn_id = str(uuid4())
    with db.connect() as c:
        action = {'type': 'start_next_task', 'voice_input_id': voice_id,
            'voice_input_binding': AgentRuntimeStore.voice_capture_snapshot(voice),
            'local_only': True, 'cloud_transmission_allowed': False}
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,?,'start_task','',?,'pending','isolated_registered_producer',?,?)''',
            (turn_id, CHILD, sid, text, json.dumps(action), utc_now(), key))
        runtime.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=turn_id, client_action_id=key,
            context_manifest={'learning_input_arrival_id': arrival['arrival_id']},
            decision=AgentPlanDecision(ObjectiveDraft('new', 'execution', 'Start original review', ('Task started',), 'execution'),
                PlannedStep(cap, {}, 'Child requests original review', cap.expected_evidence), '', False, 1))
        c.commit()
    return {'agent_turn_id': turn_id}


@pytest.mark.parametrize('entry,fault', [
    ('voice', 'source'), ('voice', 'cancel'), ('voice', 'plan'), ('voice', 'budget'),
    ('voice', 'consent'), ('voice', 'other_child'), ('voice', 'late'),
    ('direct', None), ('direct', 'source'), ('direct', 'cancel'), ('direct', 'budget'),
    ('direct', 'other_child'), ('direct', 'plan'),
])
def test_replacement_uses_original_authority_and_current_limits(media, tmp_path, monkeypatch, entry, fault):
    from datetime import timedelta
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_pet_listening_resource_admission import bind_audio
    settings, db, gateway, review, _, clock = next_day_case(media, tmp_path, monkeypatch, ready_source=entry == 'direct')
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'legacy-guard-day'}).json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        request = (direct_start(client, settings, db, gateway, monkeypatch, sid, 'guard-command') if entry == 'direct'
            else request_resume(client, settings, monkeypatch, sid, 'guard-command'))
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))]
            if fault == 'source':
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (review['diagnostic_item_id'],))
            elif fault == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (request['agent_turn_id'],))
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (request['agent_turn_id'],))
            elif fault == 'plan':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=?', (old['plan_id'],))
            elif fault == 'budget':
                c.execute('UPDATE family_rule SET max_learning_minutes=1 WHERE child_id=?', (CHILD,))
            elif fault == 'other_child':
                c.execute("UPDATE agent_plan SET child_id='child-junior-3' WHERE agent_turn_id=?", (request['agent_turn_id'],))
            c.commit()
        if fault == 'consent':
            service = PrivacyLifecycleService(db, tmp_path)
            service.update_preferences({'cloud_model_text_allowed': False})
            service.update_preferences({'cloud_model_text_allowed': True})
        if fault == 'late':
            clock.current += timedelta(days=1)
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        if fault:
            # Frozen app maps InvalidAgentPlanError to its existing typed 422;
            # main owns the optional human-readable 409 mapping for this prefix.
            assert response.status_code in {200, 409, 422}, response.text
            if response.status_code == 422:
                assert response.json()['code'] == 'AGENT_PLAN_INVALID'
            if response.status_code == 200:
                assert response.json()['execution']['result']['status'] == 'blocked', response.text
            with db.connect() as c:
                assert [tuple(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY rowid', (old['plan_id'],))] == before
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
            return
        assert response.status_code == 200, response.text
        result = response.json()['execution']['result']
        assert result['status'] == 'active', result
        assert result['plan_item_id'] != old['plan_item_id']
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['review_task_id'] == review['review_task_id'] and prompt['diagnostic_item_id'] == review['diagnostic_item_id']
        with db.connect() as c:
            media_id = c.execute('SELECT media_id FROM pet_media_binding WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0]
            operations = [r[0] for r in c.execute('SELECT operation FROM pet_media_event WHERE media_id=?', (media_id,))]
            assert operations == ['reuse_source']  # Traceable reuse, not playback or a new parental review.
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (result['plan_item_id'],)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        replay = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert replay.status_code == 200, replay.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (result['plan_item_id'],)).fetchone()[0] == 1
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (review['diagnostic_item_id'],))
            c.commit()
        invalid = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
        assert invalid.status_code == 200 and invalid.json()['status'] == 'invalidated', invalid.text

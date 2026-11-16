"""Voice hint requests retain exact local prompt ownership through restart."""
import asyncio
import json

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_agent_continuation import orchestrator_for
from test_teaching_context import flow, SECOND
from test_teaching_question_target import target_case


@pytest.mark.parametrize('utterance', ['这道题给我一个提示', '请给当前这道题一点提示', '我想要这道题的提示'])
def test_current_question_hint_freezes_original_identity_without_cloud(target_case, tmp_path, utterance):
    db, _, _, _, sid = target_case[0]
    prompt = target_case[2]
    orchestrator, gateway = orchestrator_for(db, tmp_path, {})
    result = asyncio.run(orchestrator.interpret(sid, utterance, 'hint', True))
    action = result['action']
    assert action['type'] == 'ask_tutor'
    assert action['question'] == prompt['item']['prompt']
    assert action['subject'] == prompt['item']['subject']
    assert action['diagnostic_prompt_id'] == prompt['prompt_id']
    assert action['diagnostic_item_version'] == prompt['item_version']
    assert action['local_only'] and action['cloud_transmission_allowed'] is False
    assert gateway.requests == []
    restarted, again = orchestrator_for(db, tmp_path, {})
    replay = asyncio.run(restarted.interpret(sid, utterance, 'hint', True))
    assert replay['action'] == action and again.requests == []


@pytest.mark.parametrize('utterance', ['给我讲一下分数加法', '别给这道题提示', '这道题先不讲，给另一道数学题提示', '数学给个提示'])
def test_new_question_negation_or_subject_similarity_does_not_inherit_pending_id(target_case, tmp_path, utterance):
    db, _, _, _, sid = target_case[0]
    orchestrator, gateway = orchestrator_for(db, tmp_path, {})
    result = asyncio.run(orchestrator.interpret(sid, utterance, 'other-question', True))
    assert not result['action'].get('diagnostic_prompt_id')
    assert not result['action'].get('diagnostic_item_version')
    assert gateway.requests == []


@pytest.mark.parametrize('change', ['item_version', 'cancelled', 'answer_saved', 'other_child'])
def test_hint_never_rebinds_stale_answered_or_other_child_prompt(target_case, tmp_path, change):
    db, _, _, _, sid = target_case[0]
    with db.connect() as c:
        if change == 'item_version':
            c.execute("UPDATE diagnostic_item SET prompt='Changed question' WHERE diagnostic_item_id=?", (SECOND,))
        elif change == 'other_child':
            c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                VALUES ('sibling','child-junior-3','completed','completed',datetime('now'),datetime('now'),'sibling')""")
            c.execute("UPDATE diagnostic_prompt SET child_id='child-junior-3',origin_session_id='sibling'")
        else:
            c.execute('UPDATE diagnostic_prompt SET status=?', (change,))
        c.commit()
    orchestrator, _ = orchestrator_for(db, tmp_path, {})
    result = asyncio.run(orchestrator.interpret(sid, '这道题给我一个提示', 'stale-question', False))
    assert not result['action'].get('diagnostic_prompt_id')
    assert not result['action'].get('diagnostic_item_version')


def test_local_source_hint_keeps_original_material_off_all_cloud_requests(flow, tmp_path):
    db, _, _, _, sid = flow
    private_question = 'PRIVATE local textbook question: 18 + 24 = ?'
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET source_ref='private-textbook',prompt=? WHERE diagnostic_item_id=?", (private_question, SECOND))
        c.commit()
    prompt = DiagnosticPromptService(db).prepare(sid, preferred_item_id=SECOND, client_action_id='private-question')['prompt']
    orchestrator, gateway = orchestrator_for(db, tmp_path, {})
    result = asyncio.run(orchestrator.interpret(sid, '这道题给我一个提示', 'private-hint', True))
    assert result['action']['question'] == private_question
    assert result['action']['diagnostic_prompt_id'] == prompt['prompt_id']
    assert result['action']['cloud_transmission_allowed'] is False
    assert gateway.requests == []
    with db.connect() as c:
        saved = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (result['agent_turn_id'],)).fetchone()[0])
        assert saved == result['action']


def test_playback_echo_cannot_turn_current_question_hint_into_child_authorization(target_case, tmp_path):
    db, _, _, _, sid = target_case[0]
    orchestrator, gateway = orchestrator_for(db, tmp_path, {})
    spoken = '给这道题一个提示：' + target_case[2]['item']['prompt']
    orchestrator.voice_playback.record(sid, 'hint-playback', spoken, 'started')
    assert orchestrator.voice_playback.matches(sid, 'hint-playback', spoken)
    result = asyncio.run(orchestrator.interpret(sid, spoken, 'echo-hint', True, playback_overlap_id='hint-playback'))
    assert result['intent'] == 'playback_overlap_clarification'
    assert result['transcript'] == spoken and result['response_text']
    assert result['action'].get('suppress_speech') is not True
    assert result['action']['type'] == 'none'
    assert not result['action'].get('diagnostic_prompt_id')
    assert gateway.requests == []


def test_source_change_during_local_context_lookup_never_retargets_to_new_prompt(target_case, tmp_path, monkeypatch):
    db, _, _, _, sid = target_case[0]
    original = target_case[2]
    orchestrator, gateway = orchestrator_for(db, tmp_path, {})
    replacement = []
    async def change_source(**kwargs):
        service = DiagnosticPromptService(db)
        service.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
        replacement.append(service.prepare(sid, preferred_item_id=SECOND, client_action_id='replacement')['prompt'])
        return {'status': 'unavailable', 'cloud_transmission_allowed': False}
    monkeypatch.setattr(orchestrator, '_local_atomic_learning_context', change_source)
    result = asyncio.run(orchestrator.interpret(sid, '这道题给我一个提示', 'arrival-race', True))
    assert result['action']['diagnostic_prompt_id'] == original['prompt_id']
    assert replacement[0]['prompt_id'] != original['prompt_id']
    from home_study_agent.teaching_question_target import resolve_question_target
    with db.connect() as c:
        target = resolve_question_target(c, sid, subject=result['action']['subject'], question=result['action']['question'],
            prompt_id=result['action']['diagnostic_prompt_id'], item_version=result['action']['diagnostic_item_version'])
        assert target['status'] == 'invalid' and target['question_content_cloud_blocked']
    assert gateway.requests == []


def test_actual_answer_http_handles_local_prompt_replacement_without_cloud_callback(tmp_path, monkeypatch):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    from test_app import client_for
    from test_cloud_first_agent import Planner
    gateway = Planner('diagnostic.submit', {'answer': '90'}, '90', role='answer', objective_type='learning')
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        repo = client.app.state.repository
        sid = repo.start_session('child-primary-4', 'start')['session_id']
        prompts = DiagnosticPromptService(repo.database)
        original = prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='original')['prompt']
        replacement = []
        pending = AgentRuntimeStore.pending_child_reply
        def switch(instance, session_id):
            if session_id == sid and not replacement:
                prompts.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
                replacement.append(prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-2', client_action_id='replacement')['prompt'])
            return pending(instance, session_id)
        monkeypatch.setattr(AgentRuntimeStore, 'pending_child_reply', switch)
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '90', 'client_action_id': 'arrival-answer', 'cloud_text_allowed': True})
        assert turn.status_code == 200, turn.text
        action = turn.json()['action']
        assert action['diagnostic_prompt_id'] == original['prompt_id']
        assert replacement and gateway.requests == []
        answer = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id': action['diagnostic_prompt_id'], 'item_version': action['diagnostic_item_version'],
            'answer': '90', 'client_action_id': 'old-answer'})
        assert answer.status_code == 200, answer.text
        assert answer.json()['status'] == 'cancelled'
        assert prompts.read(sid)['prompt']['prompt_id'] == replacement[0]['prompt_id']
        with repo.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('local_source', [False, True])
def test_actual_voice_to_tutor_http_preserves_binding_and_source_cloud_boundary(flow, tmp_path, local_source):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_tutor_child_model_controls import RecordingGateway
    db, _, _, _, sid = flow
    if local_source:
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET source_ref='local-textbook' WHERE diagnostic_item_id=?", (SECOND,))
            c.commit()
    prompt = DiagnosticPromptService(db).prepare(sid, preferred_item_id=SECOND, client_action_id='http-prompt')['prompt']
    gateway = RecordingGateway()
    with TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, agent_runtime_enabled=True), model_gateway=gateway)) as client:
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '这道题给我一个提示', 'client_action_id': 'voice-hint', 'cloud_text_allowed': True})
        assert turn.status_code == 200, turn.text
        action = turn.json()['action']
        assert action['diagnostic_prompt_id'] == prompt['prompt_id'] and gateway.requests == []
        reply = client.post(f'/v1/sessions/{sid}/tutor', json={
            'subject': action['subject'], 'question': action['question'], 'hint_level': action['hint_level'],
            'prompt_id': action['diagnostic_prompt_id'], 'item_version': action['diagnostic_item_version'], 'client_action_id': 'bound-hint'})
        if local_source:
            assert reply.status_code == 409, reply.text
            assert reply.json()['detail']['code'] == 'TUTOR_QUESTION_LOCAL_ONLY'
            assert gateway.requests == []
        else:
            assert reply.status_code == 200, reply.text
            assert reply.json()['output_envelope']['local_teaching_control']['question_target']['binding']['prompt_id'] == prompt['prompt_id']
            from dataclasses import asdict
            transmitted = json.dumps([asdict(request) for request in gateway.requests])
            assert prompt['prompt_id'] not in transmitted and prompt['item_version'] not in transmitted

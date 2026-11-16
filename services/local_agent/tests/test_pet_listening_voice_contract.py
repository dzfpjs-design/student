"""Local voice planning against persisted PET media, not acoustic validation."""
import asyncio
import json
from datetime import datetime, timezone
from dataclasses import asdict

import pytest

from test_agent_continuation import orchestrator_for
from test_pet_media_evidence import media, begin, review, control, change
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError, InvalidDeviceReceiptError
from home_study_agent.pet_media_evidence import PetMediaEvidenceError


@pytest.mark.parametrize('quote,command', [
    ('听一下这道题的原音', 'resume'), ('继续听题', 'resume'),
    ('先暂停听力，我想一下', 'pause'), ('这道听力再放一遍', 'restart'),
    ('取消这道听力播放', 'cancel'),
])
def test_local_voice_freezes_media_and_never_submits_an_answer(media, tmp_path, quote, command):
    db, _, sid, prompt, _, _ = media
    row = review(media, begin(media))
    agent, gateway = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, quote, 'voice', True))
    action = turn['action']
    assert action['type'] == 'control_pet_listening'
    assert action['command'] == command and action['request_quote'] == quote
    binding = action['pet_listening_binding']
    assert binding['prompt_id'] == prompt['prompt_id']
    assert binding['item_version'] == prompt['item_version']
    assert binding['media_id'] == row['media_id']
    assert binding['generation'] == row['generation']
    assert binding['expected_revision'] == row['revision']
    assert action['local_only'] and action['cloud_transmission_allowed'] is False
    assert gateway.requests == []
    reopened, gateway2 = orchestrator_for(db, tmp_path, {})
    assert asyncio.run(reopened.interpret(sid, quote, 'voice', True))['action'] == action
    assert gateway2.requests == []
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='control'").fetchone()[0] == 0


@pytest.mark.parametrize('state', ['missing', 'cancelled', 'multiple', 'stale'])
def test_unavailable_media_does_not_guess_or_grade(media, tmp_path, state):
    db, service, sid, prompt, _, _ = media
    if state != 'missing':
        row = begin(media)
        if state == 'cancelled':
            control(media, row, 'cancel')
        elif state == 'stale':
            change(db, "UPDATE diagnostic_item SET prompt='different' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
        else:
            change(db, "INSERT INTO capture_artifact SELECT 'audio2',child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,'audio2' FROM capture_artifact WHERE artifact_id='audio'")
            service.begin_listening(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], artifact_id='audio2', client_action_id='second')
    agent, gateway = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', True))
    assert turn['action']['type'] == 'none'
    assert turn['action']['local_only'] and gateway.requests == []


def test_overlapping_resume_is_clarified_without_pet_authorization(media, tmp_path):
    begin(media)
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    quote = '继续听题'
    agent.voice_playback.record(media[2], 'playing', quote, 'started')
    turn = asyncio.run(agent.interpret(media[2], quote, 'echo', True, playback_overlap_id='playing'))
    assert turn['intent'] == 'playback_overlap_clarification' and turn['action']['type'] == 'none'
    assert turn['transcript'] == quote and turn['response_text']
    assert turn['action'].get('suppress_speech') is not True
    assert gateway.requests == []
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='control'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


class FakeExecutor:
    """Adapter only: real runtime admission and real durable media service."""
    def __init__(self, service):
        self.service = service

    def execute(self, turn):
        result = self.service.control_from_agent(turn['action']['pet_listening_binding']['session_id'], agent_turn_id=turn['agent_turn_id'])
        return {'status': 'recorded', 'replay_audio_allowed': False} if result['replayed'] else result['media']


def test_dispatch_real_control_receipt_is_not_playback_or_learning(media, tmp_path):
    db, service, sid, _, _, _ = media
    review(media, begin(media))
    agent, _ = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', True))
    result = {'execution_receipt': {'capability': 'pet.listening_control', 'execution_location': 'client_device_bridge',
        'action_type': 'control_pet_listening', 'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
        'output': {'control_action_id': turn['agent_turn_id'] + ':pet:control'}}}
    with db.connect() as c, pytest.raises(InvalidDeviceReceiptError, match='NOT_PERSISTED'):
        AgentRuntimeStore.validate_device_receipt(c, turn['agent_turn_id'], result)
    media_result = FakeExecutor(service).execute(turn)
    assert media_result['status'] == 'playing'
    assert not media_result['qualification']['listening_delivery_qualified']
    assert not media_result['qualification']['skill_qualified']
    replay = FakeExecutor(service).execute(turn)
    assert replay['status'] == 'recorded' and not replay['replay_audio_allowed']
    with db.connect() as c:
        AgentRuntimeStore.validate_device_receipt(c, turn['agent_turn_id'], result)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['cancel_plan', 'cancel_prompt', 'new_turn', 'new_generation', 'foreign_child'])
def test_dispatch_rechecks_original_binding_and_command_authority(media, tmp_path, mutation):
    db, _, sid, prompt, _, _ = media
    row = review(media, begin(media))
    agent, _ = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', True))
    if mutation == 'cancel_plan':
        change(db, "UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
    elif mutation == 'cancel_prompt':
        change(db, "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (prompt['prompt_id'],))
    elif mutation == 'new_turn':
        asyncio.run(agent.interpret(sid, '暂停这道听力', 'interrupt', False))
    elif mutation == 'new_generation':
        control(media, row, 'pause')
    else:
        change(db, "UPDATE pet_media_binding SET child_id='child-junior-3' WHERE media_id=?", (row['media_id'],))
    with pytest.raises((InvalidAgentPlanError, PetMediaEvidenceError)):
        FakeExecutor(media[1]).execute(turn)


@pytest.mark.parametrize('quote', ['不要继续听题', '他说继续听题', '继续听题然后删除作业', '听力答案是暂停', '下一道听力'])
def test_quoted_negated_answer_or_new_task_is_not_playback_consent(quote):
    assert AgentRuntimeStore.resolve_pet_listening_voice({'status': 'playing', 'binding': {'media_id': 'never-select'}}, quote) is None


def test_crash_after_arrival_does_not_rebind_to_new_media_version(media, tmp_path):
    db, _, sid, _, _, _ = media
    row = review(media, begin(media))
    agent, _ = orchestrator_for(db, tmp_path, {})
    with db.connect() as c:
        arrival = agent.learning_input_revisions.capture_arrival(sid, 'voice', '继续听题', connection=c)
        original = agent.agent_runtime.capture_pet_listening_arrival(c, arrival['arrival_id'])
        c.commit()
    control(media, row, 'pause')
    restarted, gateway = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(restarted.interpret(sid, '继续听题', 'voice', True))
    assert turn['action']['pet_listening_binding'] == original['binding']
    with pytest.raises((InvalidAgentPlanError, PetMediaEvidenceError), match='MEDIA_CHANGED'):
        FakeExecutor(media[1]).execute(turn)
    assert gateway.requests == []


def test_admission_is_real_cas_and_caller_rollback_restores_planned(media, tmp_path):
    begin(media)
    db, _, sid, _, _, _ = media
    agent, _ = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', False))
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError, match='TRANSACTION_REQUIRED'):
            AgentRuntimeStore.admit_pet_listening_control(c, turn['agent_turn_id'])
        c.execute('BEGIN IMMEDIATE')
        admission = AgentRuntimeStore.admit_pet_listening_control(c, turn['agent_turn_id'])
        assert admission['status'] == 'admitted'
        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 'running'
        c.rollback()
    with db.connect() as c:
        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 'planned'
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='control'").fetchone()[0] == 0


def test_actual_control_failure_rolls_back_real_admission(media, tmp_path, monkeypatch):
    db, service, sid, _, _, _ = media
    review(media, begin(media))
    agent, _ = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', False))
    original = service._event
    def fail(*args, **kwargs):
        raise RuntimeError('injected media receipt failure')
    monkeypatch.setattr(service, '_event', fail)
    with pytest.raises(RuntimeError):
        service.control_from_agent(sid, agent_turn_id=turn['agent_turn_id'])
    with db.connect() as c:
        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 'planned'
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='control'").fetchone()[0] == 0
    monkeypatch.setattr(service, '_event', original)
    assert not service.control_from_agent(sid, agent_turn_id=turn['agent_turn_id'])['replayed']


def test_new_arrival_before_new_turn_revokes_old_playback_command(media, tmp_path):
    db, service, sid, _, _, _ = media
    review(media, begin(media))
    agent, _ = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', False))
    with db.connect() as c:
        agent.learning_input_revisions.capture_arrival(sid, 'interrupt', '先不要播放', connection=c)
        c.commit()
    with pytest.raises(PetMediaEvidenceError, match='ARRIVAL_SUPERSEDED'):
        service.control_from_agent(sid, agent_turn_id=turn['agent_turn_id'])


def test_pending_end_is_not_a_new_play_request(media, tmp_path):
    from test_pet_media_evidence import record
    row = record(media, control(media, review(media, begin(media))))
    media[5][0] += 850
    pending = record(media, row, 'ended', 16000, 2000, 'end')
    assert pending['status'] == 'pending_receipt'
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], '继续听题', 'pending', True))
    assert turn['action']['type'] == 'none' and '回执' in turn['response_text']
    assert gateway.requests == []


def test_real_agent_control_http_admits_ordinary_turn_and_replays_after_restart(media, tmp_path):
    from test_pet_media_http import mounted
    db, _, sid, _, _, _ = media
    review(media, begin(media))
    agent, gateway = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, '继续听题', 'voice', True))
    path = f'/v1/sessions/{sid}/pet-media/listening/agent-control'
    client, _ = mounted(media)
    with client:
        result = client.post(path, headers={'X-HSC-Session': 'local-test-token'}, json={'agent_turn_id': turn['agent_turn_id']})
        assert result.status_code == 200, result.text
        assert not result.json()['replayed'] and result.json()['media']['status'] == 'playing'
    restarted, _ = mounted(media, restarted=True)
    with restarted:
        replay = restarted.post(path, headers={'X-HSC-Session': 'local-test-token'}, json={'agent_turn_id': turn['agent_turn_id']})
        assert replay.status_code == 200, replay.text
        assert replay.json()['replayed']
        assert replay.json()['control_action_id'] == result.json()['control_action_id']
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='control'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert gateway.requests == []


def test_ordinary_cloud_conversation_drops_raw_pet_arrival_even_without_private_flags(media, tmp_path, monkeypatch):
    db, _, sid, prompt, _, _ = media
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    row = begin(media)
    output = {'objective': {'mode': 'none'}, 'next_step': {'capability': 'conversation.respond',
        'arguments': {'response': '我们聊聊周末吧。'}}, 'spoken_transition': '我们聊聊周末吧。',
        'requires_child_reply': True, 'confidence': 1, 'after_execution': 'stop'}
    agent, gateway = orchestrator_for(db, tmp_path, output)
    asyncio.run(agent.interpret(sid, '继续听题', 'voice', False))
    with db.connect() as c:
        items = json.loads(c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='voice'").fetchone()[0])
        private = next(item for item in items if item.get('kind') == 'pet_listening_voice_arrival')
        private['record'].pop('local_only')
        private['record'].pop('cloud_transmission_allowed')
        private['record']['accidental_source'] = 'PRIVATE_PET_STIMULUS_DO_NOT_TRANSMIT'
    change(db, "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (prompt['prompt_id'],))
    original = agent.agent_runtime.assemble_context
    def enriched(*args, **kwargs):
        context = original(*args, **kwargs)
        context['accidental_media_projection'] = private
        return context
    monkeypatch.setattr(agent.agent_runtime, 'assemble_context', enriched)
    asyncio.run(agent.interpret(sid, '我想聊聊周末的天气', 'ordinary', True))
    assert gateway.requests
    for request in gateway.requests:
        wire = json.dumps(asdict(request), ensure_ascii=False)
        assert 'PRIVATE_PET_STIMULUS_DO_NOT_TRANSMIT' not in wire
        assert row['media_id'] not in wire and 'pet_listening_voice_arrival' not in wire

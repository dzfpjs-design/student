"""Short interruption binds a local PET target, not a diagnostic answer."""
import asyncio

import pytest

from home_study_agent.pet_media_evidence import PetMediaEvidenceError
from test_agent_continuation import orchestrator_for
from test_pet_media_evidence import media, begin, review, control


@pytest.mark.parametrize('quote', ['暂停一下', '先停一下', '等我一下', '先别放了', '停一下，我想一想'])
def test_short_pause_uses_actual_agent_control_and_is_not_an_answer(media, tmp_path, quote):
    db, service, sid, _, _, _ = media
    original = control(media, review(media, begin(media)))
    agent, gateway = orchestrator_for(db, tmp_path, {})
    turn = asyncio.run(agent.interpret(sid, quote, 'short-pause', True))
    assert turn['action']['type'] == 'control_pet_listening'
    assert turn['action']['command'] == 'pause'
    assert turn['action']['request_quote'] == quote
    assert turn['action']['pet_listening_binding']['media_id'] == original['media_id']
    saved = service.control_from_agent(sid, agent_turn_id=turn['agent_turn_id'])
    assert saved['media']['status'] == 'paused' and not saved['replayed']
    assert service.control_from_agent(sid, agent_turn_id=turn['agent_turn_id'])['replayed']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert gateway.requests == []


def test_short_pause_overlap_does_not_suppress_child_interruption(media, tmp_path):
    control(media, review(media, begin(media)))
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], '暂停一下', 'overlap', True, playback_overlap_id='uncertain-voice'))
    assert turn['action']['type'] == 'control_pet_listening'
    assert turn['action']['command'] == 'pause'
    assert turn['transcript'] == '暂停一下'
    assert turn['action'].get('suppress_speech') is not True
    saved = media[1].control_from_agent(media[2], agent_turn_id=turn['agent_turn_id'])
    assert saved['media']['status'] == 'paused'
    with media[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert gateway.requests == []


def companion(agent, sid):
    return agent.agent_runtime.start_companion_activity(sid, 'interactive_story', 'local independent activity', 'other-activity')


def test_competing_activity_requires_local_clarification(media, tmp_path):
    control(media, review(media, begin(media)))
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    companion(agent, media[2])
    turn = asyncio.run(agent.interpret(media[2], '暂停一下', 'ambiguous', True))
    assert turn['action']['type'] == 'none'
    assert turn['action']['local_only'] and '哪' in turn['response_text']
    assert gateway.requests == []


def test_new_competing_activity_between_arrival_and_control_revokes_short_target(media, tmp_path):
    control(media, review(media, begin(media)))
    agent, _ = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], '暂停一下', 'race', False))
    assert turn['action']['type'] == 'control_pet_listening'
    companion(agent, media[2])
    with pytest.raises(PetMediaEvidenceError, match='FOCUS_CHANGED'):
        media[1].control_from_agent(media[2], agent_turn_id=turn['agent_turn_id'])


def test_short_pause_real_http_preserves_paused_state_after_recovery(media, tmp_path):
    from test_pet_media_http import mounted
    original = control(media, review(media, begin(media)))
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], '等我一下', 'short-http', True))
    client, _ = mounted(media)
    path = f'/v1/sessions/{media[2]}/pet-media/listening/agent-control'
    with client:
        paused = client.post(path, headers={'X-HSC-Session': 'local-test-token'}, json={'agent_turn_id': turn['agent_turn_id']})
        assert paused.status_code == 200, paused.text
        assert paused.json()['media']['status'] == 'paused'
        assert paused.json()['media']['media_id'] == original['media_id']
    reopened, _ = mounted(media, restarted=True)
    with reopened:
        replay = reopened.post(path, headers={'X-HSC-Session': 'local-test-token'}, json={'agent_turn_id': turn['agent_turn_id']})
        assert replay.status_code == 200, replay.text
        assert replay.json()['replayed'] and replay.json()['media']['status'] == 'paused'
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE client_action_id=?", (turn['agent_turn_id'] + ':pet:control',)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert gateway.requests == []


@pytest.mark.parametrize('quote', ['不要暂停', '他说暂停一下', '暂停一下再删作业', '答案是暂停一下'])
def test_quoted_negated_or_compound_request_is_not_short_pause(quote):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    assert AgentRuntimeStore.resolve_pet_listening_voice({'status': 'playing', 'binding': {}, 'short_pause_focus': 'ready'}, quote) is None

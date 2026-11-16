"""Real controlled-session services; synthetic admission, no process/media run."""
import json

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, parse_agent_plan_output
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from home_study_agent.controlled_content import ControlledContentService
from home_study_agent.controlled_game import ControlledGameService
from home_study_agent.database import Database
from test_controlled_content import seed as content_seed
from test_controlled_game import seed as game_seed


def admit(db, child, session, outcome):
    runtime = AgentRuntimeStore(db)
    args = {'operation': 'decline', 'response_quote': '这个活动先不做了'}
    decision = parse_agent_plan_output(json.dumps({'objective': {'mode': 'none'},
        'next_step': {'capability': 'companion.activity_feedback', 'arguments': args},
        'spoken_transition': '先停止这次活动。', 'requires_child_reply': False, 'confidence': .99}),
        'study', CapabilityRegistry())
    with db.connect() as c:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,tool_status,created_at,client_action_id)
            VALUES ('decline',?,?,?,'companion','',?,'synthetic','pending',datetime('now'),'decline')''',
            (child, session, args['response_quote'], json.dumps({'type': 'record_companion_activity_feedback', **args})))
        plan = runtime.persist_plan(c, child_id=child, session_id=session, agent_turn_id='decline',
            client_action_id='decline', decision=decision, context_manifest={
                'companion_activity_outcome_binding': {'outcome_id': outcome, 'expected_revision': 1,
                    'child_id': child, 'session_id': session}})
        c.execute("UPDATE agent_plan SET status='running' WHERE plan_id=?", (plan['plan_id'],))
        c.execute("UPDATE agent_step SET status='running' WHERE plan_id=?", (plan['plan_id'],))
        c.commit()


@pytest.mark.parametrize('kind', ['controlled_game', 'controlled_content'])
def test_decline_cannot_claim_external_player_or_process_stopped(tmp_path, kind):
    db = Database(tmp_path / 'controlled.db')
    db.initialize()
    if kind == 'controlled_game':
        session, item = game_seed(db, tmp_path / 'synthetic.exe')
        child = 'child-game'
        driver = ControlledGameService(db)
        driver.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        driver.approve(child, 'Game', str(tmp_path / 'synthetic.exe'), 5)
        source = driver.start(session, 'Game', 'controlled-start', item)
        source_id = source['game_session_id']
    else:
        session, resource = content_seed(db, tmp_path / 'videos')
        child, item = 'child-media', 'item-media'
        driver = ControlledContentService(db)
        driver.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        driver.approve(child, resource, 5)
        source = driver.start(session, '分数', 'controlled-start', item)
        source_id = source['content_session_id']
    with db.connect() as c:
        oid = c.execute('SELECT outcome_id FROM companion_activity_outcome WHERE source_kind=? AND source_id=?',
            (kind, source_id)).fetchone()[0]
    admit(db, child, session, oid)
    service = CompanionActivityVoiceService(db)
    result = service.execute('decline')
    assert result['activity_stop'] == {'status': 'external_stop_required', 'source_kind': kind,
        'source_id': source_id, 'previous_status': 'active', 'status_after': 'active', 'ended_at': None}
    assert not result['activity']['lifecycle']['ended']
    assert not result['activity']['habit_evidence_eligible']
    assert 'companion_activity_stopped' not in result['evidence']
    assert result['local_only'] and not result['cloud_transmission_allowed']
    assert str(tmp_path) not in json.dumps(result)
    replay = CompanionActivityVoiceService(Database(db.path)).execute('decline')
    assert replay['event'] == result['event']
    assert replay['activity_stop'] == result['activity_stop']
    assert CompanionActivityOutcomeService(db).get(child, oid)['activity']['lifecycle']['status'] == 'active'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0

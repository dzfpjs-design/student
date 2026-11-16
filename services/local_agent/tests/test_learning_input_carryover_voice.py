"""Real voice planning and durable service; dispatch is an explicit test adapter."""
import asyncio
import json
from dataclasses import asdict

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError, parse_agent_plan_output, CapabilityRegistry
from home_study_agent.agent_runtime import AgentPlanDecision, ObjectiveDraft, PlannedStep
from home_study_agent.learning_input_carryover import CarryoverConflict
from test_learning_input_carryover import carry, schedule, inputs, flow, CHILD, plan
from test_agent_continuation import orchestrator_for


def say(agent, text, key):
    return asyncio.run(agent.interpret('today', text, key, True))


class FakeExecutor:
    """Only the host dispatch adapter is fake; it calls the real SQLite service."""
    def __init__(self, service):
        self.service = service

    def execute(self, turn):
        with self.service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT t.*,p.plan_id,p.context_manifest_json,s.step_id,s.idempotency_key,s.arguments_json
                FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0''', (turn['agent_turn_id'],)).fetchone()
            c.execute("UPDATE agent_plan SET status='running' WHERE plan_id=? AND status='planned'", (row['plan_id'],))
            c.execute("UPDATE agent_step SET status='running' WHERE step_id=? AND status='planned'", (row['step_id'],))
            binding = json.loads(row['context_manifest_json'])['learning_input_unplaced_binding']
            args = json.loads(row['arguments_json'])
            result = self.service.review_unplaced(row['child_id'], binding['obligation_id'], row['session_id'],
                args['decision'], binding['revision'], binding['source_version'], row['idempotency_key'],
                expected_context_version=binding['context_version'], authorized_turn_id=row['agent_turn_id'],
                request_quote=args['request_quote'], not_before=args.get('not_before'), connection=c)
            payload = {'capability': 'learning_inputs.review_unplaced', 'status': 'completed',
                'response_text': f"实际安排{result['allocated_minutes']}分钟，剩余{result['remaining_minutes']}分钟。",
                'result': result, 'local_only': True, 'cloud_transmission_allowed': False}
            c.execute("UPDATE agent_step SET status='completed',result_json=? WHERE step_id=?", (json.dumps(payload), row['step_id']))
            c.execute("UPDATE agent_plan SET status='completed' WHERE plan_id=?", (row['plan_id'],))
            c.commit()
            return result


@pytest.mark.parametrize('utterance,decision', [('今天继续', 'continue_today'), ('不要了', 'drop'), ('明天再做', 'keep')])
def test_focus_natural_decision_real_plan_dispatch_and_restart(carry, tmp_path, utterance, decision):
    db, service, _, _ = carry
    plan(db)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    question = say(agent, '昨天没排完的作业有哪些', 'list')
    assert question['runtime_capability'] == 'conversation.respond'
    selected = say(agent, '第一项', 'select')
    assert '口算' in selected['response_text']
    restarted, other_gateway = orchestrator_for(db, tmp_path, {})
    turn = say(restarted, utterance, 'review')
    assert turn['action']['type'] == 'review_learning_input_unplaced'
    assert turn['action']['request_quote'] == utterance
    assert turn['action']['decision'] == decision
    assert turn['action']['cloud_transmission_allowed'] is False
    with db.connect() as c:
        stored = c.execute('SELECT plan_json,context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        assert json.loads(stored['context_manifest_json'])['learning_input_unplaced_binding']['obligation_id'] == carry[2]
        assert json.loads(stored['plan_json'])['after_execution'] == 'stop'
    result = FakeExecutor(service).execute(turn)
    assert result['allocated_minutes'] == (30 if decision == 'continue_today' else 0)
    assert result['status'] == {'continue_today': 'scheduled', 'drop': 'dropped', 'keep': 'kept'}[decision]
    assert FakeExecutor(service).execute(turn) == result
    assert say(restarted, utterance, 'review')['agent_turn_id'] == turn['agent_turn_id']
    assert not gateway.requests and not other_gateway.requests


@pytest.mark.parametrize('carry', [{'two': True}], indirect=True)
def test_multiple_candidates_require_selection_then_fresh_decision(carry, tmp_path):
    db, service, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    question = say(agent, '昨天没排完的作业今天继续', 'list')
    assert question['runtime_capability'] == 'conversation.respond'
    assert '第1项' in question['response_text'] and '第2项' in question['response_text']
    focus = say(agent, '第二项', 'select')
    assert focus['runtime_capability'] == 'conversation.respond'
    assert '应用题' in focus['response_text']
    turn = say(agent, '明天再做', 'review')
    assert turn['runtime_capability'] == 'learning_inputs.review_unplaced'
    assert FakeExecutor(service).execute(turn)['status'] == 'kept'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
    assert not gateway.requests


@pytest.mark.parametrize('change', ['source', 'plan', 'cancel'])
def test_dispatch_rejects_changed_source_plan_or_cancelled_authority(carry, tmp_path, change):
    db, service, _, _ = carry
    plan(db)
    agent, _ = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    say(agent, '第一项', 'select')
    turn = say(agent, '今天继续', 'review')
    with db.connect() as c:
        if change == 'source':
            c.execute("UPDATE homework_item SET title='different' WHERE homework_id='math-a'")
        elif change == 'plan':
            c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='today-plan'")
        else:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
        c.commit()
    with pytest.raises(CarryoverConflict):
        FakeExecutor(service).execute(turn)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM learning_input_carryover_event').fetchone()


def test_stale_focus_cannot_select_new_version_by_same_ordinal(carry, tmp_path):
    db, _, _, _ = carry
    plan(db)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    with db.connect() as c:
        c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='today-plan'")
        c.commit()
    answer = say(agent, '第一项', 'select')
    assert answer['runtime_capability'] == 'conversation.respond'
    assert '版本已变化' in answer['response_text']
    assert not gateway.requests


@pytest.mark.parametrize('extra', [{'obligation_id': 'fake'}, {'expected_revision': 0}, {'child_id': CHILD}])
def test_model_cannot_supply_target_or_revision(extra):
    payload = {'objective': {'mode': 'none'}, 'next_step': {'capability': 'learning_inputs.review_unplaced',
        'arguments': {'decision': 'drop', 'request_quote': '不要了', **extra}}, 'spoken_transition': '核对',
        'requires_child_reply': False, 'confidence': 1, 'after_execution': 'stop'}
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(payload), 'study', CapabilityRegistry())


@pytest.mark.parametrize('utterance', ['不要了，明天再做', '不要今天继续', '先别取消安排', '改天再做'])
def test_negation_or_conflicting_decisions_are_clarified_not_executed(carry, tmp_path, utterance):
    db, _, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    turn = say(agent, utterance, 'ambiguous')
    assert turn['runtime_capability'] == 'conversation.respond'
    assert turn['action']['cloud_transmission_allowed'] is False
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM learning_input_carryover_event').fetchone()
    assert not gateway.requests


@pytest.mark.parametrize('change', ['continuation', 'quote', 'binding', 'automatic', 'child', 'foreign_arrival'])
def test_runtime_rejects_forged_or_automatic_review_even_without_prompt_boundary(carry, tmp_path, change):
    db, _, _, _ = carry
    agent, _ = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    turn = say(agent, '今天继续', 'review')
    cap = CapabilityRegistry().require('learning_inputs.review_unplaced', 'homework_capture')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        manifest = json.loads(row[0])
        args = {'decision': 'continue_today', 'request_quote': '今天继续'}
        after = 'stop'
        if change == 'continuation':
            c.execute("UPDATE agent_turn SET intent='runtime_continuation' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
        elif change == 'quote':
            args['request_quote'] = '明天再做'
        elif change == 'binding':
            manifest['learning_input_unplaced_binding']['obligation_id'] = 'invented'
        elif change == 'automatic':
            after = 'replan'
        elif change == 'child':
            manifest['learning_input_unplaced_binding']['child_id'] = 'child-junior-3'
        else:
            manifest['learning_input_arrival_id'] = c.execute("SELECT arrival_id FROM learning_input_arrival WHERE client_action_id='list'").fetchone()[0]
        decision = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execution'),
            PlannedStep(cap, args, 'forged', cap.expected_evidence), '核对', False, 1, after_execution=after)
        with pytest.raises(InvalidAgentPlanError):
            agent.agent_runtime.persist_plan(c, child_id=CHILD, session_id='today', agent_turn_id=turn['agent_turn_id'],
                client_action_id='review', decision=decision, context_manifest=manifest)
        c.rollback()


def test_arrival_retry_preserves_original_focus_not_fresh_plan_version(carry, tmp_path, monkeypatch):
    db, service, _, _ = carry
    plan(db)
    agent, _ = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    def crash(*args, **kwargs):
        raise RuntimeError('after arrival before plan commit')
    monkeypatch.setattr(agent, '_persist_unplaced_voice', crash)
    with pytest.raises(RuntimeError):
        say(agent, '今天继续', 'review')
    with db.connect() as c:
        frozen = c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='review'").fetchone()[0]
        c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='today-plan'")
        c.commit()
    restarted, gateway = orchestrator_for(db, tmp_path, {})
    turn = say(restarted, '今天继续', 'review')
    with pytest.raises(CarryoverConflict):
        FakeExecutor(service).execute(turn)
    with db.connect() as c:
        assert c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='review'").fetchone()[0] == frozen
    assert not gateway.requests


def test_playback_overlap_cannot_authorize_drop(carry, tmp_path, monkeypatch):
    db, _, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    monkeypatch.setattr(agent.voice_playback, 'matches', lambda *args: True)
    turn = asyncio.run(agent.interpret('today', '不要了', 'echo', True, playback_overlap_id='playing'))
    assert turn['intent'] == 'playback_overlap_clarification'
    assert turn['transcript'] == '不要了' and turn['response_text']
    assert turn['action'].get('suppress_speech') is not True
    assert turn.get('runtime_capability') != 'learning_inputs.review_unplaced'
    assert not gateway.requests


def test_interruption_removes_focus_and_cloud_cannot_reconstruct_target(carry, tmp_path):
    db, _, _, _ = carry
    output = {'objective': {'mode': 'none'}, 'next_step': {'capability': 'learning_inputs.review_unplaced',
        'arguments': {'decision': 'drop', 'request_quote': '不要了'}}, 'spoken_transition': '取消',
        'requires_child_reply': False, 'confidence': 1, 'after_execution': 'stop'}
    agent, gateway = orchestrator_for(db, tmp_path, output)
    say(agent, '昨天没排完的作业有哪些', 'list')
    say(agent, '换个话题', 'interrupt')
    turn = say(agent, '不要了', 'unfocused')
    assert turn.get('runtime_capability') != 'learning_inputs.review_unplaced'
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM learning_input_carryover_event').fetchone()
    for request in gateway.requests:
        wire = json.dumps(request.messages, ensure_ascii=False)
        assert carry[2] not in wire and 'unplaced_voice_arrival' not in wire


def test_changed_mind_revokes_only_same_obligation_undispatched_command(carry, tmp_path):
    db, service, _, _ = carry
    plan(db)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    first = say(agent, '今天继续', 'continue')
    second = say(agent, '不要了', 'drop')
    assert second['runtime_capability'] == 'learning_inputs.review_unplaced'
    assert second['action']['decision'] == 'drop'
    with pytest.raises(CarryoverConflict):
        FakeExecutor(service).execute(first)
    assert FakeExecutor(service).execute(second)['status'] == 'dropped'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE plan_id='today-plan'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
    assert not gateway.requests


def test_initial_single_candidate_still_names_source_before_generic_decision(carry, tmp_path):
    db, _, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    result = say(agent, '昨天没排完的作业今天继续', 'first')
    assert result['runtime_capability'] == 'conversation.respond'
    assert '口算' in result['response_text']
    assert say(agent, '今天继续', 'confirm')['runtime_capability'] == 'learning_inputs.review_unplaced'
    assert not gateway.requests


def test_explicit_unique_title_can_bind_arrival_without_model_target(carry, tmp_path):
    db, service, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    turn = say(agent, '昨天没安排的口算今天继续', 'named')
    assert turn['runtime_capability'] == 'learning_inputs.review_unplaced'
    result = FakeExecutor(service).execute(turn)
    assert result['status'] == 'accepted_for_planning' and result['allocated_minutes'] == 0
    assert result['target_homework_id'] and result['remaining_minutes'] == 30
    assert not gateway.requests


def test_non_homework_synonyms_and_relative_keep_date_stay_local(carry, tmp_path):
    db, service, _, _ = carry
    agent, gateway = orchestrator_for(db, tmp_path, {})
    first = say(agent, '昨天那几道没做完今天补上', 'start')
    assert first['runtime_capability'] == 'conversation.respond'
    assert '口算' in first['response_text']
    paused = say(agent, '剩下的口算先不做了', 'pause')
    assert paused['runtime_capability'] == 'conversation.respond'
    kept = say(agent, '先留着后天再说', 'keep')
    assert kept['action']['decision'] == 'keep'
    assert kept['action']['not_before'] == '2026-09-10'
    assert FakeExecutor(service).execute(kept)['status'] == 'kept'
    assert not gateway.requests


def test_temporary_deferral_clarifies_and_revokes_pending_write_without_dropping_work(carry, tmp_path):
    db, service, _, _ = carry
    plan(db)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    pending = say(agent, '今天继续', 'continue')
    paused = say(agent, '剩下的口算先不做了', 'temporary')
    assert paused['runtime_capability'] == 'conversation.respond'
    with pytest.raises(CarryoverConflict):
        FakeExecutor(service).execute(pending)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM learning_input_carryover_event').fetchone()
    assert service.list_unplaced(CHILD, 'today')['items'][0]['remaining_minutes'] == 30
    assert not gateway.requests


def test_already_committed_allocation_is_not_cancelled_by_late_reply(carry, tmp_path):
    db, service, _, _ = carry
    plan(db)
    agent, _ = orchestrator_for(db, tmp_path, {})
    say(agent, '昨天没排完的作业有哪些', 'list')
    first = say(agent, '今天继续', 'continue')
    result = FakeExecutor(service).execute(first)
    say(agent, '剩下的口算先不做了', 'temporary')
    with db.connect() as c:
        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (first['agent_turn_id'],)).fetchone()[0] == 'completed'
        assert c.execute('SELECT COUNT(*) FROM learning_input_carryover_event').fetchone()[0] == 1
    assert FakeExecutor(service).execute(first) == result


@pytest.mark.parametrize('carry', [{'private_markers': True}], indirect=True)
@pytest.mark.parametrize('extra_context', [False, True])
def test_unrelated_real_cloud_request_never_contains_local_arrival_snapshots(carry, tmp_path, monkeypatch, extra_context):
    db, _, _, _ = carry
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    output = {'objective': {'mode': 'none'}, 'next_step': {'capability': 'conversation.respond',
        'arguments': {'response': '可以，我们聊聊周末的天气。'}}, 'spoken_transition': '可以，我们聊聊周末的天气。',
        'requires_child_reply': True, 'confidence': 1, 'after_execution': 'stop'}
    agent, gateway = orchestrator_for(db, tmp_path, output)
    say(agent, '昨天没排完的作业有哪些', 'list')
    with db.connect() as c:
        snapshot = json.loads(c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='list'").fetchone()[0])
        private = next(item for item in snapshot if item['kind'] == 'unplaced_voice_arrival')
        assert 'LOCAL_TITLE_PRIVATE_7cd' in json.dumps(private, ensure_ascii=False)
        artifact = dict(c.execute("SELECT * FROM capture_artifact WHERE artifact_id='private-artifact'").fetchone())
        assert 'LOCAL_STEM_PRIVATE_8ab' in artifact['extracted_text']
    if extra_context:
        assemble = agent.agent_runtime.assemble_context
        def enriched(*args, **kwargs):
            context = assemble(*args, **kwargs)
            # Defense against future raw local projection wiring, without relying on flags.
            context['learning_input_unplaced'] = {**private['record'], 'source': artifact}
            context['diagnostic_accidental_local_record'] = {**private, 'source': artifact}
            return context
        monkeypatch.setattr(agent.agent_runtime, 'assemble_context', enriched)
    say(agent, '我想聊聊周末的天气', 'ordinary')
    assert gateway.requests and any(request.request_type == 'agent_plan' for request in gateway.requests)
    for request in gateway.requests:
        wire = json.dumps(asdict(request), ensure_ascii=False)
        for secret in ('LOCAL_TITLE_PRIVATE_7cd', 'LOCAL_STEM_PRIVATE_8ab', 'LOCAL_EVIDENCE_PRIVATE_9bc',
                       carry[2], 'unplaced_voice_arrival', 'learning_input_unplaced_binding'):
            assert secret not in wire

"""Write-boundary contract tests; the shared host must still wire the hook."""
import asyncio

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.repository import StudyRepository
from test_cross_stage_voice_ownership import orchestrator_for, execute
from test_exam_simulation import _database
from test_pet_vocabulary_prompt import flow


@pytest.fixture
def pending(tmp_path):
    db, sid = _database(tmp_path / 'answer-write-gate.db')
    DocumentMonitoringService(db)
    service = ExamSimulationService(db)
    service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'real-exam-start')
    agent, gateway = orchestrator_for(db, tmp_path, {})
    original = '我先把两个数合起来，再把多的部分去掉'
    source = asyncio.run(agent.interpret(sid, original, 'source-candidate', False))
    assert source['intent'] == 'pending_answer_clarification'
    execute(agent, db, source)
    confirmed = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'explicit-confirmation', False))
    assert confirmed['action']['type'] == 'submit_exam_answer'
    assert confirmed['action']['answer'] == original
    assert not gateway.requests
    return db, sid, agent, source, confirmed, original


def resolve(db, turn_id):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        value = AgentRuntimeStore.resolve_local_answer_submission(c, turn_id, 'exam.submit')
        c.rollback()
        return value


def test_real_confirmation_resolves_original_words_not_confirmation_phrase(pending):
    db, _, _, source, confirmed, original = pending
    value = resolve(db, confirmed['agent_turn_id'])
    assert value['status'] == 'confirmed' and value['answer'] == original
    assert value['source_turn_id'] == source['agent_turn_id']
    assert value['local_only'] and not value['cloud_transmission_allowed']
    assert resolve(Database(db.path), confirmed['agent_turn_id']) == value
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['source_cancel', 'target_version', 'new_turn', 'switch_child', 'forged_answer', 'forged_confirmation'])
def test_cancellation_and_changes_after_planning_block_new_writes(pending, change):
    db, sid, agent, source, confirmed, _ = pending
    if change == 'new_turn':
        asyncio.run(agent.interpret(sid, '今天计划做到哪了', 'new-command-arrival', False))
    elif change == 'switch_child':
        StudyRepository(db).switch_active_session_child(sid, 'child-junior-3', 'actual-profile-switch')
    else:
        with db.connect() as c:
            if change == 'source_cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (source['agent_turn_id'],))
            elif change == 'target_version':
                c.execute("UPDATE specialized_exam_item SET source_version='revised' WHERE position=1")
            elif change == 'forged_answer':
                c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.answer','forged') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (confirmed['agent_turn_id'],))
            elif change == 'forged_confirmation':
                c.execute("UPDATE agent_turn SET transcript='我没有确认答案' WHERE agent_turn_id=?", (confirmed['agent_turn_id'],))
            c.commit()
    with pytest.raises(InvalidAgentPlanError, match='ANSWER_'):
        resolve(db, confirmed['agent_turn_id'])
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_failed_turn_retry_keeps_same_source_without_resubmission(pending):
    db, sid, agent, _, confirmed, original = pending
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='failed' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (confirmed['agent_turn_id'],))
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?", (confirmed['agent_turn_id'],))
        c.execute("UPDATE agent_turn SET tool_status='failed' WHERE agent_turn_id=?", (confirmed['agent_turn_id'],))
        c.commit()
    restored = AgentRuntimeStore(Database(db.path))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert restored.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'exam.submit')['answer'] == original
        c.rollback()
    replay = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'explicit-confirmation', False))
    assert replay['agent_turn_id'] == confirmed['agent_turn_id']
    assert replay['action']['answer'] == original


def test_lost_clarification_receipt_is_not_source_cancellation(pending):
    db, _, _, source, confirmed, original = pending
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET tool_status='failed' WHERE agent_turn_id=?", (source['agent_turn_id'],))
        c.execute("UPDATE agent_plan SET status='failed' WHERE agent_turn_id=?", (source['agent_turn_id'],))
        c.commit()
    assert resolve(db, confirmed['agent_turn_id'])['answer'] == original


def test_write_gate_requires_host_transaction_and_matching_capability(pending):
    db, _, _, _, confirmed, _ = pending
    with db.connect() as c:
        with pytest.raises(InvalidAgentPlanError, match='WRITE_TRANSACTION_REQUIRED'):
            AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'exam.submit')
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(InvalidAgentPlanError, match='CHILD_TURN_REQUIRED'):
            AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'diagnostic.submit')
        c.rollback()


def test_vocabulary_confirmation_returns_saved_meaning_with_exact_card_binding(flow, tmp_path):
    db, service, sid = flow
    card = service.present_next(sid)
    agent, gateway = orchestrator_for(db, tmp_path, {})
    original = '这个词是在说一个可以借书的地方'
    candidate = asyncio.run(agent.interpret(sid, original, 'vocabulary-candidate', False))
    assert candidate['intent'] == 'pending_answer_clarification'
    execute(agent, db, candidate)
    confirmed = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'vocabulary-confirmation', False))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'pet_vocabulary.submit')
        assert result['answer'] == original
        assert result['binding'] == {key: card[key] for key in ('prompt_session_id', 'card_version')}
        c.rollback()
    assert service.active_prompt(sid) == card
    assert not gateway.requests

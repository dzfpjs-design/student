"""The public borrowed-transaction writer cannot revive erased source payloads."""
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.agent_orchestrator import InvalidAgentTurnOutcomeError
from test_native_runtime_source_cleanup import native, prompt_command, snapshot, CHILD


@pytest.mark.parametrize('marker', ['turn', 'step'])
@pytest.mark.parametrize('succeeded', [False, True])
def test_borrowed_complete_refuses_each_erasure_marker_before_any_write(native, marker, succeeded):
    db, _, item, _, sid = native
    turn, _ = prompt_command(db, CHILD, sid, item['diagnostic_item_id'])
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if marker == 'turn':
            c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (turn,))
        else:
            c.execute("UPDATE agent_step SET error_code='SOURCE_DELETED' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        before = snapshot(c, turn)
        with pytest.raises(InvalidAgentTurnOutcomeError, match='SOURCE_DELETED'):
            runtime.complete_turn_execution(c, turn, succeeded, 'NETWORK_ERROR', {'cached_question': 'must not be written'})
        assert c.in_transaction and snapshot(c, turn) == before
        c.rollback()

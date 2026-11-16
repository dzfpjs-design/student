"""Real probe and dispatcher tests independent of app/OCR imports."""
import asyncio

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.repository import StudyRepository
from test_agent_continuation import orchestrator_for
from test_mistake_dialogue_diagnosis import opened, speak
from test_mistake_gate_reliability import CHILD, flow


def execute(agent, db, turn):
    receipt = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn['agent_turn_id'])
    agent.complete_turn(turn['agent_turn_id'], receipt['response_text'], 'completed', None, receipt)
    return receipt


@pytest.mark.parametrize('change', ['none', 'pause', 'item_version', 'source_cancel'])
def test_probe_candidate_is_local_and_confirmation_requires_current_source(flow, tmp_path, change, monkeypatch):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    probe = speak(dialogue, sid, '不是粗心，我不知道为什么错', 'probe-reason')
    assert probe['phase'] == 'diagnosis_probe'
    agent, gateway = orchestrator_for(db, tmp_path, {})
    original = '我先把两个数合起来，再乘最后的数'
    candidate = asyncio.run(agent.interpret(sid, original, 'probe-candidate', True))
    assert candidate['intent'] == 'pending_answer_clarification'
    execute(agent, db, candidate)
    confirmed = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'probe-confirmation', True))
    assert confirmed['action']['utterance'] == original
    if change == 'pause':
        speak(dialogue, sid, '跳过', 'external-pause')
    with db.connect() as c:
        if change == 'item_version':
            item = probe['state']['next_activity']['item']['diagnostic_item_id']
            c.execute("UPDATE diagnostic_item SET prompt=prompt || ' revised' WHERE diagnostic_item_id=?", (item,))
        elif change == 'source_cancel':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (candidate['agent_turn_id'],))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        if change == 'none':
            resolved = AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'mistake.dialogue')
            assert resolved['answer'] == original and resolved['child_id'] == CHILD
        else:
            with pytest.raises(InvalidAgentPlanError, match='ANSWER_'):
                AgentRuntimeStore.resolve_local_answer_submission(c, confirmed['agent_turn_id'], 'mistake.dialogue')
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert not gateway.requests
    if change == 'none':
        from home_study_agent.mistake_dialogue import MistakeDialogueService
        score = MistakeDialogueService._score_operation
        errors = []
        def capture_error(self, *args):
            try:
                return score(self, *args)
            except Exception as error:
                errors.append(repr(error))
                raise
        monkeypatch.setattr(MistakeDialogueService, '_score_operation', capture_error)
        execute(agent, db, confirmed)
        assert not errors, errors
        with db.connect() as c:
            attempts = c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchall()
            assert [row[0] for row in attempts] == [original]

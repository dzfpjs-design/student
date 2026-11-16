"""Actual SQLite dispatch and prompt lineage, separate from Runtime admission."""
from copy import deepcopy

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_teaching import WritingTeachingService
from test_writing_response_origin import writing, CHILD
from test_writing_teaching import ready


def step_for(writing, kind):
    service, _, _ = ready(writing)
    db, sid, _, parent, _ = writing
    resolved = service.resolve_next_action(sid, prompt_id=parent['prompt_id'],
        item_version=parent['item_version'], kind=kind,
        diagnostic_item_id='diag-pet-writing-2' if kind == 'independent_check' else None)
    assert resolved['status'] == 'ready'
    executor = AgentCapabilityExecutor.__new__(AgentCapabilityExecutor)
    executor.database = db
    executor.repository = StudyRepository(db)
    step = {'capability_name': 'writing.independent_check' if kind == 'independent_check' else 'writing.revise_original',
        'session_id': sid, 'arguments': {}, 'idempotency_key': 'writing-followup-' + kind,
        'agent_turn_id': 'dispatch-only', 'response_text': '',
        'action': {'writing_followup_binding': {key: resolved[key] for key in ('action', 'binding_version')}}}
    return executor, step


@pytest.mark.parametrize('kind', ['revision', 'independent_check'])
def test_dispatch_prepares_exact_followup_and_retries_without_another_attempt(writing, kind, monkeypatch):
    executor, step = step_for(writing, kind)
    def inspect(c, turn_id):
        assert c.in_transaction and turn_id == 'dispatch-only'
        return {'binding': deepcopy(step['action']['writing_followup_binding'])}
    monkeypatch.setattr(AgentRuntimeStore, 'inspect_writing_followup_execution', staticmethod(inspect), raising=False)
    first = executor._dispatch(step)
    assert first['result']['status'] == 'waiting_answer'
    prompt = first['result']['prompt']
    assert prompt['prompt_id'] != writing[3]['prompt_id']
    assert prompt['diagnostic_item_id'] == step['action']['writing_followup_binding']['action']['diagnostic_item_id']
    assert first['result']['local_only'] and not first['result']['cloud_transmission_allowed']
    assert executor._dispatch(step)['result']['prompt']['prompt_id'] == prompt['prompt_id']
    with writing[0].connect() as c:
        lineage = c.execute('SELECT * FROM writing_followup WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
        assert lineage['kind'] == kind and lineage['child_id'] == CHILD
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    restored = WritingTeachingService(writing[0]).read(writing[1],
        prompt_id=writing[3]['prompt_id'], item_version=writing[3]['item_version'])
    assert any(item['prompt_id'] == prompt['prompt_id'] for item in restored['followups'])


@pytest.mark.parametrize('change', ['binding', 'kind', 'source'])
def test_dispatch_never_reselects_or_prepares_from_a_changed_binding(writing, change):
    executor, original = step_for(writing, 'independent_check')
    step = deepcopy(original)
    bound = step['action']['writing_followup_binding']
    if change == 'binding':
        bound['binding_version'] = 'changed'
    elif change == 'kind':
        bound['action']['kind'] = 'revision'
    else:
        with writing[0].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",
                (bound['action']['diagnostic_item_id'],))
            c.commit()
    result = executor._dispatch(step)
    assert result['result']['status'] == 'blocked' and result['evidence'] == []
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1


def test_authorization_revoked_at_write_boundary_leaves_no_followup(writing, monkeypatch):
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    executor, step = step_for(writing, 'revision')
    def revoked(c, turn_id):
        assert c.in_transaction and turn_id == 'dispatch-only'
        raise InvalidAgentPlanError('ANSWER_WRITING_FOLLOWUP_CANCELLED')
    monkeypatch.setattr(AgentRuntimeStore, 'inspect_writing_followup_execution', staticmethod(revoked))
    with pytest.raises(InvalidAgentPlanError, match='CANCELLED'):
        executor._dispatch(step)
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1

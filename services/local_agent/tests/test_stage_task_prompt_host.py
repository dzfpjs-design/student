"""Real task dispatch must use the stage plan's original question and receipt."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.repository import StudyRepository, PlanInputError
from test_stage_execution import persisted_plan
from test_specialized_stages import case, CHILD, answer, baseline
from test_goal_task_prompt import command


def scheduled(case):
    binding = persisted_plan(case)
    db, _, sid, _ = case
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='pending' WHERE plan_item_id='stage-plan-item'")
        c.commit()
    turn = command((db, sid, 'stage-plan-item', binding['knowledge_node_id'], None), child_id=CHILD)
    return binding, turn


def test_cold_stage_rule_check_is_read_only(case):
    from home_study_agent.goal_roadmap import goal_catalog_source_reason
    scheduled(case)
    db, _, sid, _ = case
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
        c.execute('PRAGMA query_only=ON')
        assert goal_catalog_source_reason(c, sid, 'stage-plan-item') is None
        assert '\n'.join(c.iterdump()) == before


@pytest.mark.parametrize('missing', ['goal_program', 'goal_program_revision'])
def test_partial_goal_rule_schema_cannot_authorize_stage(case, missing):
    from home_study_agent.goal_program import GoalProgramService
    from home_study_agent.goal_roadmap import goal_catalog_source_reason
    scheduled(case)
    db, _, sid, _ = case
    GoalProgramService(db).ensure_schema()
    with db.connect() as c:
        c.execute('DROP TABLE ' + missing)
        c.commit()
        c.execute('PRAGMA query_only=ON')
        assert goal_catalog_source_reason(c, sid, 'stage-plan-item') == 'catalog_source_unbound'


def test_stage_voice_start_presents_exact_original_and_restart_keeps_one_prompt(case):
    binding, turn = scheduled(case)
    db, _, sid, _ = case
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    assert result['result']['status'] == 'active'
    prompt = result['result']['diagnostic_prompt']['prompt']
    assert prompt['diagnostic_item_id'] == binding['diagnostic_item_id']
    assert prompt['item']['prompt'] in result['response_text']
    assert result['requires_child_reply'] is True
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(turn) == result
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_assessment_run'").fetchone():
            assert not c.execute('SELECT 1 FROM specialized_assessment_run').fetchone()


def test_stage_unavailable_original_never_starts_generic_timer(case):
    binding, turn = scheduled(case)
    db = case[0]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (binding['diagnostic_item_id'],))
        c.commit()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    assert result['result']['status'] == 'blocked'
    assert isinstance(result['response_text'], str) and result['response_text'].strip()
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='stage-plan-item'").fetchone()[0] == 'pending'
        assert not c.execute('SELECT 1 FROM diagnostic_prompt').fetchone()


def test_stage_rechecks_source_between_question_preparation_and_timer(case, monkeypatch):
    binding, turn = scheduled(case)
    db, _, sid, _ = case
    repo = StudyRepository(db)
    start = repo.start_plan_item
    def withdraw(*args, **kwargs):
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (binding['diagnostic_item_id'],))
            c.commit()
        return start(*args, **kwargs)
    monkeypatch.setattr(repo, 'start_plan_item', withdraw)
    with pytest.raises(PlanInputError):
        AgentCapabilityExecutor(db, repo).execute(turn)
    with db.connect() as c:
        prompt = dict(c.execute('SELECT * FROM diagnostic_prompt').fetchone())
        assert not c.execute("SELECT 1 FROM plan_item_event WHERE event_type='started'").fetchone()
    result = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='90', client_action_id=str(uuid4()))
    assert result['status'] == 'invalidated'


def test_unrelated_stage_answer_cannot_complete_original_plan_then_exact_receipt_can(case):
    binding, turn = scheduled(case)
    db, _, sid, clock = case
    repo = StudyRepository(db)
    result = AgentCapabilityExecutor(db, repo).execute(turn)
    clock[0] = datetime.now(timezone.utc)
    answer(case, baseline(case))
    with pytest.raises(PlanInputError):
        repo.complete_plan_item('stage-plan-item', 15, 'unrelated-complete')
    prompt = result['result']['diagnostic_prompt']['prompt']
    with db.connect() as c:
        item = c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?', (binding['diagnostic_item_id'],)).fetchone()
    submitted = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer=json.loads(item[0])['exact_answers'][0], client_action_id='exact-stage-answer')
    assert submitted['status'] == 'answered'
    clock[0] = datetime.now(timezone.utc)
    completed = repo.complete_plan_item('stage-plan-item', 15, 'exact-complete')
    assert completed['status'] == 'completed'


def test_direct_legacy_start_cannot_skip_original_question_binding(case):
    scheduled(case)
    with pytest.raises(PlanInputError):
        StudyRepository(case[0]).start_plan_item('stage-plan-item', 'legacy-generic-start')


def test_new_voice_turn_resumes_paused_original_question_without_replacing_it(case):
    binding, turn = scheduled(case)
    db, _, sid, _ = case
    repo = StudyRepository(db)
    original = AgentCapabilityExecutor(db, repo).execute(turn)
    repo.pause_plan_item('stage-plan-item', 'pause-original-stage', 'child_requested_pause')
    resumed_turn = command((db, sid, 'stage-plan-item', binding['knowledge_node_id'], None), child_id=CHILD)
    resumed = AgentCapabilityExecutor(Database(db.path), StudyRepository(Database(db.path))).execute(resumed_turn)
    assert resumed['result']['status'] == 'active'
    assert resumed['result']['diagnostic_prompt']['prompt']['prompt_id'] == original['result']['diagnostic_prompt']['prompt']['prompt_id']
    assert resumed['requires_child_reply'] is True
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1

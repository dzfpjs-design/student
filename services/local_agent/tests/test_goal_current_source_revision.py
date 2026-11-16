"""New routes can use reviewed replacements; old plans never adopt them."""
import hashlib
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService, source_state
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_typed_knowledge_source_import import source

CHILD = 'child-junior-3'


def attach_and_repair(case):
    db, sid, pid, node, _ = case
    text = json.dumps(source(), ensure_ascii=False)
    old = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
        title='Synthetic route source', subject='物理', grade_band='初三',
        license_status='family_owned_index', parent_reviewed=True, version_label='1',
        source_ref='isolated:route-source', content_text=text))
    with db.connect() as c:
        c.execute('UPDATE knowledge_node SET source_ref=? WHERE knowledge_node_id=?', (old['source_ref'], node))
        c.execute('UPDATE knowledge_excerpt SET content_text=?,content_hash=? WHERE source_id=?',
                  (text[:1000], hashlib.sha256(text[:1000].encode()).hexdigest(), old['source_id']))
        c.commit()
    routes = GoalRoadmapService(db)
    initial = routes.read(CHILD, 'junior_exam_readiness')
    assert node in {v['knowledge_node_id'] for v in initial['prerequisite_path']}
    revision = KnowledgeSourceRevisionService(db)
    preview = revision.preview(CHILD, old['source_id'], text)
    revised = revision.revise(CHILD, old['source_id'], content_text=text,
        expected_version=preview['source_version'], client_action_id='repair-route-source')
    return old, revised['current']['source_id'], revision


def approve(db, revision, source_id):
    with db.connect() as c:
        _, _, version = source_state(c, CHILD, source_id)
    revision.review(CHILD, source_id, expected_version=version, client_action_id='review-route-source')


@pytest.mark.parametrize('reviewed,legacy', [(False,False), (True,False), (True,True)])
def test_current_route_and_legacy_plan_remain_separate_after_repair(execution, reviewed, legacy):
    db, sid, pid, node, _ = execution
    if legacy:
        with db.connect() as c:
            rows = c.execute("SELECT decision_id,metadata_json FROM plan_constraint_decision WHERE obligation_type='junior_exam_goal'").fetchall()
            for row in rows:
                value = json.loads(row['metadata_json'])
                value.get('planning_evidence', {}).pop('catalog_binding', None)
                c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?', (json.dumps(value), row['decision_id']))
            c.commit()
    old, new_id, revision = attach_and_repair(execution)
    if reviewed:
        approve(db, revision, new_id)
    route = GoalRoadmapService(Database(db.path)).read(CHILD, 'junior_exam_readiness')
    assert (node in {v['knowledge_node_id'] for v in route['prerequisite_path']}) is reviewed
    result = GoalRoadmapService(Database(db.path)).resolve_execution(sid, pid)
    assert result['status'] == 'blocked', 'An old plan must not silently use a reviewed replacement'
    if legacy and reviewed:
        assert result['reason_code'] == 'catalog_source_unbound'
    with db.connect() as c:
        assert c.execute('SELECT current_version FROM knowledge_source WHERE source_id=?', (old['source_id'],)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def reviewed_new_plan(execution):
    db, sid, pid, node, _ = execution
    _, new_id, revision = attach_and_repair(execution)
    approve(db, revision, new_id)
    route = GoalProgramService(db).roadmap(CHILD, 'junior_exam_readiness')
    assert route['next_daily_action'], route['risks']
    basis = route['next_daily_action'].get('catalog_binding')
    assert basis and new_id in basis['source_ids']
    # Use the production plan generator in a fresh session, not a metadata edit.
    from home_study_agent.repository import StudyRepository
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.execute("UPDATE daily_plan SET status='cancelled' WHERE session_id=?", (sid,))
        c.execute("UPDATE plan_item SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)", (sid,))
        c.commit()
    current = repo.start_session(CHILD, 'new-source-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?", (current,))
        c.commit()
    repo.add_homework(current, '数学', 'Synthetic school work', 10, 'written', None, None, None, None, None, 'new-work')
    workspace = repo.generate_plan(current, 'new-source-plan')
    item = next(v for v in workspace['plan_items'] if v['source_type']=='junior_exam_goal')
    fresh = GoalRoadmapService(db).resolve_execution(current, item['plan_item_id'])
    assert fresh['status'] == 'ready', fresh
    assert fresh['binding']['catalog_binding']['source_ids'] == basis['source_ids']
    assert GoalRoadmapService(Database(db.path)).resolve_execution(current, item['plan_item_id']) == fresh
    return (db, current, item['plan_item_id'], node, execution[4]), new_id


def test_new_plan_binds_reviewed_source_and_later_change_blocks(execution):
    case, new_id = reviewed_new_plan(execution)
    db, current, pid, _, _ = case
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET version_label='changed-after-plan' WHERE source_id=?", (new_id,))
        c.commit()
    changed = GoalRoadmapService(db).resolve_execution(current, pid)
    assert changed['status'] == 'blocked' and changed['reason_code'] == 'catalog_source_changed'


def test_prepared_original_and_resume_recheck_catalog_without_resuming_or_scoring(execution):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository, PlanInputError
    from test_goal_task_prompt import command
    case, source_id = reviewed_new_plan(execution)
    db, sid, pid, _, _ = case
    repo = StudyRepository(db)
    result = AgentCapabilityExecutor(db, repo).execute(command(case))
    prompt = result['result']['diagnostic_prompt']['prompt']
    repo.pause_plan_item(pid, 'source-test-pause')
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET version_label='changed-after-prompt' WHERE source_id=?", (source_id,))
        c.commit()
    read = DiagnosticPromptService(Database(db.path)).read(sid, prompt_id=prompt['prompt_id'])
    assert read['status']=='invalidated', 'A prepared original must not outlive its roadmap source binding'
    with pytest.raises(PlanInputError) as failure:
        StudyRepository(Database(db.path)).resume_plan_item(pid, 'source-test-resume')
    assert failure.value.reason_code == 'catalog_source_changed'
    assert '家长' in str(failure.value)
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0]=='paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed' AND plan_item_id=?", (pid,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_source_recheck_is_inside_actual_timer_transaction_and_failure_rolls_back(execution, monkeypatch):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository, PlanInputError
    from test_goal_task_prompt import command
    case, source_id = reviewed_new_plan(execution)
    db, _, pid, _, _ = case
    repo = StudyRepository(db)
    turn = command(case)
    original = repo._validate_pet_task_start
    def change_inside_transaction(c, item, turn_id=None):
        original(c, item, turn_id)
        assert c.in_transaction
        c.execute("UPDATE knowledge_source SET version_label='transaction-fault' WHERE source_id=?", (source_id,))
    with monkeypatch.context() as patch:
        patch.setattr(repo, '_validate_pet_task_start', change_inside_transaction)
        with pytest.raises(PlanInputError) as failure:
            AgentCapabilityExecutor(db, repo).execute(turn)
        assert failure.value.reason_code == 'catalog_source_changed'
    with db.connect() as c:
        assert c.execute('SELECT version_label FROM knowledge_source WHERE source_id=?', (source_id,)).fetchone()[0]=='1'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?", (pid,)).fetchone()[0]==0
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0]=='pending'


def test_other_child_source_metadata_does_not_change_this_catalog(execution):
    case, _ = reviewed_new_plan(execution)
    db, sid, pid, _, _ = case
    before = GoalRoadmapService(db).resolve_execution(sid, pid)
    KnowledgeBaseService(db).import_source('child-primary-4', dict(source_type='school_material',
        title='Other child source', subject='数学', grade_band='四年级', license_status='family_owned_index',
        parent_reviewed=True, version_label='other', source_ref='isolated:route-source', content_text='Synthetic other child material'))
    after = GoalRoadmapService(Database(db.path)).resolve_execution(sid, pid)
    assert after['status']=='ready'
    assert after['binding'] == before['binding']


def test_answered_original_receipt_is_preserved_when_route_source_later_changes(execution):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from test_goal_task_prompt import command
    case, source_id = reviewed_new_plan(execution)
    db, sid, _, _, _ = case
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(command(case))
    prompt = result['result']['diagnostic_prompt']['prompt']
    prompts = DiagnosticPromptService(db)
    answered = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                              answer='90', client_action_id='actual-original-answer')
    assert answered['status']=='answered', answered
    with db.connect() as c:
        original = tuple(c.execute('SELECT * FROM diagnostic_attempt').fetchone())
        c.execute("UPDATE knowledge_source SET version_label='after-original-response' WHERE source_id=?", (source_id,))
        c.commit()
    read = DiagnosticPromptService(Database(db.path)).read(sid, prompt_id=prompt['prompt_id'])
    assert read['status']=='answered'
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM diagnostic_attempt').fetchone()) == original
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1

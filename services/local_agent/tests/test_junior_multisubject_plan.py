"""Real plan endpoints with synthetic reported mistakes; no mastery fabrication."""
import pytest

from home_study_agent.database import Database
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.mistake_book import MistakeBookService
from test_goal_program import _client, _headers, _advance_to_plan, _qualified_goal_fixture


CHILD = 'child-junior-3'
TARGET = 'node-j3-math-discriminant'


@pytest.mark.parametrize('node,subject,has_source,budget', [
    ('node-j3-chinese-reading-evidence', '语文', True, 90),
    ('node-j3-physics-experiment-reasoning', '物理', True, 90),
    ('node-j3-math-quadratic-formula', '数学', True, 90),
    ('node-j3-chinese-reading-evidence', '语文', False, 90),
    ('node-j3-chinese-reading-evidence', '语文', True, 40),
    (TARGET, '数学', True, 90),
])
def test_review_represents_its_node_not_every_junior_goal(tmp_path, node, subject, has_source, budget):
    with _client(tmp_path) as client:
        headers = _headers(client)
        db = Database(tmp_path / 'goals.db')
        if has_source:
            _qualified_goal_fixture(db)
        else:
            from test_school_oral_catalog import without_authored_tasks
            without_authored_tasks(db, TARGET)
        rule = client.put(f'/v1/parent/children/{CHILD}/family-rule', headers=headers, json=dict(
            latest_end_time=None, max_learning_minutes=budget, task_segment_minutes=35,
            break_minutes=5, pet_min_minutes=0, competition_minutes=0,
            observation_mode='shadow', reminder_budget_per_10m=0))
        assert rule.status_code == 200, rule.text
        service = MistakeBookService(db)
        mistake = service.capture(child_id=CHILD, knowledge_node_id=node, subject=subject,
            title='Synthetic reported misunderstanding', prompt_summary='Synthetic source pending diagnosis',
            submitted_answer='I need to review my reasoning', source_type='manual', source_ref=None,
            confidence=.8, client_action_id='cross-subject-capture')
        service.confirm(mistake['mistake_id'], CHILD, 'confirm', 'cross-subject-confirm')
        roadmap = client.get(f'/v1/parent/children/{CHILD}/goal-programs/junior_exam_readiness/roadmap',
                             headers=headers)
        assert roadmap.status_code == 200, roadmap.text
        assert roadmap.json()['next_daily_action']['knowledge_node_id'] == TARGET
        planned = _advance_to_plan(client, CHILD)
        workspace = planned['workspace']
        reviews = [row for row in workspace['plan_items'] if row['source_type'] == 'mistake_review'
                   and row['source_ref'] == mistake['mistake_id']]
        assert len(reviews) == 1, workspace
        goals = [row for row in workspace['plan_items'] if row['source_type'] == 'junior_exam_goal']
        owed = [row for row in workspace['constraint_decisions']
                if row['obligation_type'] == 'junior_exam_goal' and row['decision'] == 'deferred']
        if node == TARGET:
            assert not goals and not owed
        elif has_source and budget == 90:
            assert len(goals) == 1 and not owed, workspace['constraint_decisions']
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            # Planning a second skill must not start it or invent improvement.
            assert goals[0]['started_at'] is None
        else:
            assert not goals and len(owed) == 1, workspace['constraint_decisions']
            basis = owed[0]['metadata']['planning_evidence']
            assert basis['knowledge_node_id'] == TARGET
            assert bool(basis.get('school_supply_gap')) is (not has_source)
        assert sum(row['expected_minutes'] for row in workspace['plan_items']
                   if row['status'] not in {'rescheduled','cancelled'}) <= budget
        sid = planned['session_id']
        expected_ids = {row['plan_item_id'] for row in workspace['plan_items']}
    # Full backend recreation, no source/answer updates between reads.
    with _client(tmp_path) as restarted:
        saved = restarted.get(f'/v1/sessions/{sid}')
        assert saved.status_code == 200, saved.text
        assert {row['plan_item_id'] for row in saved.json()['plan_items']} == expected_ids
        assert [row for row in saved.json()['constraint_decisions']
                if row['obligation_type'] == 'junior_exam_goal' and row['decision'] == 'deferred'] == owed
        if goals:
            execution = GoalRoadmapService(Database(tmp_path / 'goals.db')).resolve_execution(sid, goals[0]['plan_item_id'])
            assert execution['status'] == 'ready', execution
            assert execution['binding']['knowledge_node_id'] == TARGET
            assert execution['diagnostic_item_id'] and execution['mastery_claim_allowed'] is False

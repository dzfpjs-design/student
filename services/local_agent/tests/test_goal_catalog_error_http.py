import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from home_study_agent.goal_roadmap import goal_catalog_source_reason
from test_goal_current_source_revision import reviewed_new_plan
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_goal_task_prompt import command


@pytest.mark.parametrize('operation', ['start', 'resume'])
def test_actual_plan_http_source_change_has_child_recovery_text(execution, operation):
    case, source_id = reviewed_new_plan(execution)
    db, sid, pid, _, _ = case
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        if operation == 'resume':
            result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(command(case))
            assert result['result']['diagnostic_prompt']['status'] == 'waiting_answer'
            StudyRepository(db).pause_plan_item(pid,'source-http-pause')
        with db.connect() as c:
            c.execute("UPDATE knowledge_source SET version_label='http-new-source' WHERE source_id=?", (source_id,))
            before = c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]
            c.commit()
        response = client.post(f'/v1/plan-items/{pid}/{operation}', json={'client_action_id':'source-http-request'})
        assert response.status_code == 409, response.text
        body = response.json()
        assert body['code'] == 'GOAL_CATALOG_SOURCE_CHANGED'
        assert 'catalog_source_' not in body['message']
        assert '家长' in body['message'] and '重新安排' in body['message']
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0] == before
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE client_action_id=?',('source-http-request',)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('bad', [None, [], 7, {'planning_evidence': []}, {'planning_evidence': {'knowledge_node_id': []}}])
def test_bad_metadata_shape_blocks_only_its_exact_task(execution, bad):
    db,sid,pid,_,_=execution
    with db.connect() as c:
        item=c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()
        c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE plan_id=? AND obligation_type=?',
            (json.dumps(bad),item['plan_id'],item['source_type']))
        assert goal_catalog_source_reason(c,sid,pid)=='catalog_source_unbound'
        other=c.execute("SELECT plan_item_id FROM plan_item WHERE plan_id=? AND source_type='school_homework'",(item['plan_id'],)).fetchone()
        assert other and goal_catalog_source_reason(c,sid,other[0]) is None

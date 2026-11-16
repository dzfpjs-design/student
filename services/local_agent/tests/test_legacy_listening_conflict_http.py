"""HTTP error presentation only; real task guards have separate journey tests."""
from fastapi.testclient import TestClient
from datetime import datetime, timezone
import pytest

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import InvalidAgentPlanError
from home_study_agent.task_start_binding import TaskStartBindingError
from test_diagnostic_prompt_http import environment, start, CHILD


@pytest.mark.parametrize('error_type', [InvalidAgentPlanError, TaskStartBindingError])
def test_legacy_task_conflicts_are_bounded_explanations_without_retry_or_learning_writes(environment, monkeypatch, error_type):
    settings, db = environment
    app = create_app(settings)
    code = ['TASK_START_LEGACY_REVIEW_BUDGET_INSUFFICIENT']
    kind = [error_type]

    def reject(_turn_id):
        raise kind[0](code[0])

    monkeypatch.setattr(app.state.agent_capability_executor, 'execute', reject)
    expected = {
        'BUDGET_INSUFFICIENT': '不挤占休息时间',
        'BUDGET_UNKNOWN': '学习时间',
        'AUTHORIZATION_CHANGED': '许可',
        'PLAN_CHANGED': '安排',
        'VOICE_REQUIRED': '确认',
        'VOICE_CHANGED': '请求',
        'ACTIVE_TASK': '正在进行',
        'PENDING_QUESTION': '等待回答',
        'CHANGED': '原练习',
    }
    with TestClient(app) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
                response_text,action_json,orchestrator_version,created_at,client_action_id)
                VALUES ('adapter-only-turn',?,?,'synthetic command','test','','{}','test',?,'adapter-only-turn')''',
                (CHILD,sid,datetime.now(timezone.utc).isoformat()))
            c.commit()
            before = {name: c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                for name in ('agent_turn','agent_plan','diagnostic_attempt','plan_item','plan_item_event')}
        for suffix, phrase in expected.items():
            code[0] = 'TASK_START_LEGACY_REVIEW_' + suffix
            result = client.post('/v1/agent-turns/adapter-only-turn/execute')
            assert result.status_code == 409, result.text
            body = result.json()
            payload = body.get('detail', body)
            assert payload['code'] == code[0]
            assert phrase in payload['message'] and len(payload['message']) <= 240
            assert '你可以再试一次' not in payload['message']
        code[0] = 'TASK_START_LEGACY_REVIEW_PRIVATE_INTERNAL_DETAIL'
        kind[0] = InvalidAgentPlanError
        unknown = client.post('/v1/agent-turns/adapter-only-turn/execute')
        assert unknown.status_code == 422
        assert unknown.json()['code'] == 'AGENT_PLAN_INVALID'
        assert 'PRIVATE_INTERNAL_DETAIL' not in unknown.text
        with db.connect() as c:
            assert {name: c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in before} == before

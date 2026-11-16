"""HTTP presentation contract only; execution guards have separate journey tests."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import InvalidAgentPlanError
from test_diagnostic_prompt_http import environment, start, CHILD


def test_oral_conflicts_explain_next_action_without_learning_or_retry(environment, monkeypatch):
    settings, db = environment
    app = create_app(settings)
    code = ['ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED']

    def reject(_turn_id):
        raise InvalidAgentPlanError(code[0])

    monkeypatch.setattr(app.state.agent_capability_executor, 'execute', reject)
    expected = {
        'SOURCE_CHANGED': '材料', 'CONSENT_CHANGED': '许可',
        'GOAL_CHANGED': '学习目标', 'PROMPT_CHANGED': '题目',
        'CANCELLED': '取消', 'FAILED_COMMAND': '没有重新执行',
        'COMMAND_REQUIRED': '确认', 'COMMAND_CHANGED': '请求',
        'VOICE_CHANGED': '请求', 'AUTHORITY_CHANGED': '会话',
    }
    with TestClient(app) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
                response_text,action_json,orchestrator_version,created_at,client_action_id)
                VALUES ('oral-adapter-turn',?,?,'synthetic command','test','','{}','test',?,'oral-adapter-turn')''',
                (CHILD, sid, datetime.now(timezone.utc).isoformat()))
            c.commit()
            before = {table: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('agent_turn', 'agent_plan', 'diagnostic_prompt', 'diagnostic_attempt', 'plan_item_event')}
        for suffix, phrase in expected.items():
            code[0] = 'ANSWER_ORAL_FOLLOWUP_' + suffix
            response = client.post('/v1/agent-turns/oral-adapter-turn/execute')
            assert response.status_code == 409, response.text
            body = response.json()
            assert body['code'] == code[0]
            assert phrase in body['message'] and len(body['message']) <= 240
            assert '评分' not in body['message']
            assert '你可以再试一次' not in body['message']
        code[0] = 'ANSWER_ORAL_FOLLOWUP_PRIVATE_INTERNAL_DETAIL'
        unknown = client.post('/v1/agent-turns/oral-adapter-turn/execute')
        assert unknown.status_code == 422
        assert unknown.json()['code'] == 'AGENT_PLAN_INVALID'
        assert 'PRIVATE_INTERNAL_DETAIL' not in unknown.text
        with db.connect() as c:
            assert {table: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in before} == before

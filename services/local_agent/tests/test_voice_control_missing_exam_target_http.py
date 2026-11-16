"""A classifier cannot restore a nonexistent exam target."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_content_free_voice_control_http import ControlGateway, task_fixture
from test_diagnostic_prompt_http import environment
from test_voice_stop_activity_arbitration_http import session, say


def test_model_exam_controls_without_exam_clarify_and_do_not_dispatch(environment):
    settings, db = environment
    quote = '刚才那份卷子先处理一下'
    gateway = ControlGateway(quote, 'exam.pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        repo = task_fixture(db, sid)
        repo.pause_plan_item('control-task-1', 'paused-for-control-test')
        for control in ('exam.pause', 'exam.resume', 'exam.cancel'):
            gateway.control = control
            turn = say(client, sid, quote, f'missing-{control}')
            assert turn['action']['type'] == 'none'
            assert 'exam_run_id' not in turn['action']
            result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert result.status_code == 200, result.text
            assert result.json()['execution']['capability'] == 'conversation.respond'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM specialized_exam_run').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()[0] == 'paused'

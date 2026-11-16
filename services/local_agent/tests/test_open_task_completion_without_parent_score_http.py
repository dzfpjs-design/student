"""A child can finish an answered task without turning candidate feedback into a score.

Real HTTP/SQLite/restart; speech, planning and formative inference are synthetic.
No human rubric approval or calibration authority is supplied.
"""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_cloud_first_agent import Planner
from test_open_response_spoken_runtime_http import (
    test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey,
)
from test_voice_runtime_binding import capture, interpret


def test_finish_original_open_task_without_parent_grading_and_preserve_unknown_score(tmp_path, monkeypatch):
    inspected = []

    def finish(j):
        client, db, sid = j['client'], j['db'], j['sid']
        assert j['follow']['status'] == 'feedback_required'
        feedback = client.post(f"/v1/agent-turns/{j['follow']['feedback_turn_id']}/execute")
        assert feedback.status_code == 200, feedback.text
        assert feedback.json()['execution']['result']['formative_only']
        with db.connect() as c:
            task = dict(c.execute("SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE dp.session_id=? AND pi.status='active'", (sid,)).fetchone())
            assert task['source_type'] == 'junior_exam_goal'
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        text = '这项做完了'
        # Declare a synthetic planner's intended finish control, rather than
        # letting the reused start-task fixture return a fresh start command.
        original = j['gateway'].complete

        async def complete(request):
            if request.request_type == 'agent_plan':
                return await Planner('task.complete', {}, text, objective_type='execution').complete(request)
            return await original(request)

        monkeypatch.setattr(j['gateway'], 'complete', complete)
        j['command_state'].update(control='task.complete', quote=text)
        voice = capture(client, j['settings'], monkeypatch, sid, text, key='finish-open-voice', child=j['child'])
        turn = interpret(client, sid, text, 'finish-open-command', voice)
        assert turn['runtime_capability'] == 'task.complete', turn
        path = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
        response = client.post(path)
        assert response.status_code == 200, response.text
        assert response.json()['execution']['result']['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?', (j['attempt']['attempt_id'],)).fetchone()[0] is None
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
            assert not c.execute('SELECT 1 FROM mastery_state WHERE child_id=? AND knowledge_node_id=? AND evidence_count>0', (j['child'], j['source']['knowledge_node_id'])).fetchone()
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            replay = restarted.post(path)
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['result']['status'] == 'completed'
            with db.connect() as c:
                assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'completed'
                assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'", (task['plan_item_id'],)).fetchone()[0] == 1
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
                assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        inspected.append(task['plan_item_id'])

    run_journey(tmp_path, monkeypatch, 'authored:数学', None, True, None, after_saved=finish)
    assert len(inspected) == 1

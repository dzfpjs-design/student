"""Receipt proof uses real HTTP/Runtime and synthetic trusted ASR only."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.exam_simulation import ExamSimulationService
from test_diagnostic_prompt_http import environment
from test_natural_task_navigation_http import Offline
from test_pending_answer_task_navigation_http import setup, say, execute, unchanged_prompt


@pytest.mark.parametrize('mode', ['typed','voice'])
def test_actual_completed_pause_receipt_has_exact_task_write(environment, monkeypatch, mode):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, '暂停任务', 'receipt-proof', mode)
        result = execute(client, heard)
        with db.connect() as c:
            proof = {'input':'暂停任务','input_mode':mode,'action_source':heard['action'].get('source'),
                'runtime_capability':heard['runtime_capability'],'executed_capability':result['execution']['capability'],
                'tool_status':c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()[0],
                'waiting_question':c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0],
                'paused_events':c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0],
                'original_task_status':repo._get_plan_item_by_id('control-task-1')['status']}
        assert heard['runtime_capability'] == 'task.pause', json.dumps(proof,ensure_ascii=False,sort_keys=True)
        assert proof['executed_capability'] == 'task.pause' and proof['tool_status'] == 'completed'
        assert proof['paused_events'] == 1 and proof['original_task_status'] == 'paused'
        unchanged_prompt(client, db, sid, prompt)


@pytest.mark.parametrize('change',['question_cancel','question_version','new_arrival','child','exam'])
def test_replay_device_admission_rejects_old_owner_before_playback(environment, monkeypatch, change):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline()),raise_server_exceptions=False) as client:
        sid, repo, prompt = setup(client, db)
        heard = say(client, settings, monkeypatch, sid, prompt, '再读一遍这道题', 'frozen-replay', 'typed')
        assert heard['runtime_capability'] == 'diagnostic.replay', heard
        if change == 'new_arrival':
            say(client, settings, monkeypatch, sid, prompt, '不要重读这道题', 'replay-retracted', 'typed')
        elif change == 'exam':
            ExamSimulationService(db).start(sid,'competition_math','数学','practice',2,30,'competing-after-replay')
        else:
            with db.connect() as c:
                if change == 'question_cancel':
                    c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?",(prompt['prompt_id'],))
                elif change == 'question_version':
                    c.execute("UPDATE diagnostic_prompt SET item_version='changed-original' WHERE prompt_id=?",(prompt['prompt_id'],))
                else:
                    c.execute("UPDATE daily_session SET child_id='child-junior-3' WHERE session_id=?",(sid,))
                c.commit()
        admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{heard['agent_turn_id']}/control-admission")
        assert admitted.status_code == 409, admitted.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0

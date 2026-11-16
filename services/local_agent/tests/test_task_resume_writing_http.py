"""Real due-writing start/pause/resume retains one original typed question."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from test_agent_continuation import Gateway
from test_voice_runtime_binding import capture, interpret
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('cancel_origin', [False, True])
def test_real_writing_review_voice_resume_reuses_prompt_and_rechecks_original_command(writing_chain, monkeypatch, cancel_origin):
    db, old_sid = writing_chain[:2]
    _, far, _ = complete_transfer_chain(writing_chain)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='completed',status='completed',ended_at=? WHERE session_id=?",
            (far['attempt']['created_at'],old_sid))
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (far['attempt']['created_at'],CHILD))
        c.commit()
    clock = advance_clock(monkeypatch,datetime.fromisoformat(far['attempt']['created_at'])+timedelta(days=8))
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan,'utc_now',lambda:clock.now(timezone.utc))
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD,'resume-writing-review-day')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?",(sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''',(CHILD,))
        c.commit()
    workspace = repo.generate_plan(sid,'resume-writing-review-plan')
    item = next(p for p in workspace['plan_items'] if p['source_type']=='due_review')
    repo.transition(sid,'plan_review','study','Synthetic daily plan accepted','resume-plan-accepted')
    gateway = Gateway({
        'utterance_binding':{'role':'task_request','quote':'开始今天复查'},
        'objective':{'mode':'new','type':'execution','summary':'开始今天复查',
            'success_criteria':['原复查题已呈现'],'phase':'execute'},
        'next_step':{'capability':'task.start','arguments':{'start_selector':{
            'subject':None,'task_kind':'review','source_type':None,'target_quote':'开始今天复查'}}},
        'spoken_transition':'开始今天的复查。','requires_child_reply':False,
        'after_execution':'stop','confidence':.99})
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        started = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':'开始今天复查','client_action_id':'resume-start-review'})
        assert started.status_code == 200, started.text
        original_turn = started.json()['agent_turn_id']
        result = client.post(f'/v1/agent-turns/{original_turn}/execute')
        assert result.status_code == 200, result.text
        prompt = result.json()['execution']['result']['diagnostic_prompt']['prompt']
        quote = '先暂停这项写作，保留题目'
        gateway.output = {'role':'control','control':'task.pause','quote':quote,'confidence':.99}
        voice = capture(client,settings,monkeypatch,sid,quote,prompt,'pause-writing-capture')
        paused = interpret(client,sid,quote,'pause-writing-task',voice)
        response = client.post(f"/v1/agent-turns/{paused['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        quote = '刚才停下的作文我接着写，计时也接上'
        gateway.output = {'role':'control','control':'task.resume','quote':quote,'confidence':.99,
            'selector': {'subject': None, 'task_kind': None, 'relative_order': None}}
        voice = capture(client,settings,monkeypatch,sid,quote,prompt,'resume-writing-capture')
        heard = interpret(client,sid,quote,'resume-writing-task',voice)
        assert heard['action']['type']=='resume_paused_task', heard
        if cancel_origin:
            # Inject cancellation of the original command, not the new resume:
            # this distinguishes the source guard from the latest-turn fence.
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(original_turn,))
                c.commit()
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == (409 if cancel_origin else 200), response.text
        if not cancel_origin:
            value = response.json()['execution']['result']
            restored = value['diagnostic_prompt']
            assert restored['status']=='waiting_answer'
            assert restored['prompt']['prompt_id']==prompt['prompt_id']
            assert restored['prompt']['item_version']==prompt['item_version']
            assert restored['prompt']['review_task_id']==item['source_ref']
            assert response.json()['execution']['requires_child_reply']
            replay = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert replay.status_code==200, replay.text
            assert replay.json()['execution']['result']['replayed']
            assert 'diagnostic_prompt' not in replay.json()['execution']['result']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'",
                (item['plan_item_id'],)).fetchone()[0] == (0 if cancel_origin else 1)
    for request in gateway.requests:
        if request.request_type == 'voice_interaction':
            assert set(json.loads(request.messages[-1]['content'])) == {'transcript','state'}
            assert prompt['item']['prompt'] not in json.dumps(request.messages)
            assert prompt['prompt_id'] not in json.dumps(request.messages)

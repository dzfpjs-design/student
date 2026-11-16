"""Synthetic reviewed manuscripts/clock, real Agent HTTP and dispatcher, no live model."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from test_agent_continuation import Gateway
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('state', ['cancel', 'paused', 'answered'])
def test_natural_start_due_writing_review_uses_exact_goal_prompt_and_cancel(writing_chain, monkeypatch, state):
    db, old_sid = writing_chain[:2]
    _, far, _ = complete_transfer_chain(writing_chain)
    # Same completed prior-session setup as the host journey; not a claim that
    # its synthetic reviewer goals establish retained mastery.
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
    sid = repo.start_session(CHILD,'voice-writing-review-day')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?",(sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''',(CHILD,))
        c.commit()
    workspace = repo.generate_plan(sid,'voice-writing-review-plan')
    item = next(p for p in workspace['plan_items'] if p['source_type']=='due_review')
    assert workspace['session']['state']=='plan_review'
    repo.transition(sid,'plan_review','study','Synthetic daily plan accepted','review-plan-accepted')
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
        heard = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':'开始今天复查','client_action_id':'voice-start-review','cloud_text_allowed':True})
        assert heard.status_code==200, heard.text
        turn = heard.json()
        assert turn.get('runtime_capability')=='task.start', turn
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code==200, executed.text
        result = executed.json()['execution']['result']
        assert result['status']=='active', result
        assert len(gateway.requests)==1
        prompt = result['diagnostic_prompt']['prompt']
        assert prompt['review_task_id']==item['source_ref']
        assert prompt['child_id']==CHILD and prompt['origin_session_id']==sid
        with db.connect() as c:
            request = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                (turn['agent_turn_id'],)).fetchone()[0])
            bound = request['goal_prompt_binding']
            assert bound['prompt_id']==prompt['prompt_id'] and bound['plan_item_id']==item['plan_item_id']
            assert bound['stage']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
        assert read.status_code==200 and read.json()['status']=='waiting_answer', read.text
        if state=='paused':
            quote = '暂停这个任务，先保留我的进度'
            gateway.output = {'role':'control','control':'task.pause','quote':quote,'confidence':.99}
            heard_pause = client.post(f'/v1/sessions/{sid}/agent-turn',json={
                'transcript':quote,'client_action_id':'pause-writing-review','cloud_text_allowed':True})
            assert heard_pause.status_code==200, heard_pause.text
            assert heard_pause.json().get('runtime_capability')=='task.pause', heard_pause.text
            paused = client.post(f"/v1/agent-turns/{heard_pause.json()['agent_turn_id']}/execute")
            assert paused.status_code==200 and paused.json()['execution']['result']['status']=='paused', paused.text
            waiting = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert waiting.status_code==200 and waiting.json()['status']=='waiting_answer', waiting.text
            # Resume transport uses the real plan API here, not an untested
            # claim that the content-free voice lane handles task.resume.
            resumed = client.post(f"/v1/plan-items/{item['plan_item_id']}/resume",json={'client_action_id':'resume-writing-review'})
            assert resumed.status_code==200, resumed.text
            waiting = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert waiting.status_code==200 and waiting.json()['status']=='waiting_answer', waiting.text
        elif state=='answered':
            from home_study_agent.writing_response_origin import WritingOriginalService
            text = 'Dear Ben, our science club meets on Tuesdays in the library. I want to build a tall bridge with my friends because we enjoy designing useful things. Please join us next Tuesday and bring your ideas. Best wishes, Mia.'
            original = WritingOriginalService(db).capture_native_response(sid,prompt_id=prompt['prompt_id'],
                item_version=prompt['item_version'],text=text,client_action_id='review-native-original')
            saved = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
                'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'answer':text,
                'writing_original_artifact_id':original['artifact_id'],'client_action_id':'review-native-answer'})
            assert saved.status_code==200 and saved.json()['status']=='answered', saved.text
            attempt_id = saved.json()['attempt']['attempt_id']
        stop = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':'别讲了','client_action_id':'voice-cancel-review','cloud_text_allowed':False})
        assert stop.status_code==200, stop.text
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
        with db.connect() as c:
            states = [dict(r) for r in c.execute('''SELECT t.intent,p.status,s.status AS step_status,o.status AS goal_status
                FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                LEFT JOIN agent_objective o USING(objective_id) WHERE t.session_id=?''',(sid,))]
        expected = 'answered' if state=='answered' else 'invalidated'
        assert read.status_code==200 and read.json()['status']==expected, (read.json()['status'],stop.json()['action'],states)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?',(item['source_ref'],)).fetchone()[0]==int(state=='answered')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
        assert read.status_code==200 and read.json()['status']==expected, read.text
        if state=='answered':
            assert read.json()['attempt']['attempt_id']==attempt_id
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1

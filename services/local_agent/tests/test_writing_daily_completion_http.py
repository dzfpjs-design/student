"""A new day's real manuscript must discharge the original practice, not certify it."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_writing_next_day_journey import environment, next_day_ready, CHILD, OTHER, capture, interpret
from test_writing_followup_paper_http import revised_page
from test_writing_feedback_focus_http import assert_specific_feedback


@pytest.fixture
def completion_ready(next_day_ready,monkeypatch):
    case=next_day_ready
    started=case.client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
    assert started.status_code==200,started.text
    prompt=started.json()['execution']['result']['diagnostic_prompt']['prompt']
    case.journey.sid=case.sid
    monitor=case.client.post(f'/v1/sessions/{case.sid}/document-monitor/start',
        json={'client_action_id':'complete-daily-monitor'})
    assert monitor.status_code==200,monitor.text
    feedback,result=case.journey.submit(case.client,prompt,'complete-daily-manuscript',*revised_page(monkeypatch,'revision'))
    assert_specific_feedback(result)
    with case.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==2
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',
            (case.task['plan_item_id'],)).fetchone()[0]=='active', 'Feedback does not authorize task completion.'
    command=case.journey.say(case.client,'这项写作复习做完了','complete-daily-explicit','task.complete',prompt)
    assert command['action']['type']=='complete_active_task',command
    semantic=next(r for r in reversed(case.journey.gateway.requests) if r.request_type=='voice_interaction')
    transmitted=json.dumps({'messages':semantic.messages,'context':semantic.context_manifest},ensure_ascii=False)
    for private in (prompt['prompt_id'],case.commitment['commitment_id'],case.task['plan_item_id'],
                    'daily_writing_completion',prompt['item']['prompt']):
        assert private not in transmitted
    case.completion_command,case.completion_prompt,case.feedback_turn=command,prompt,feedback
    case.followup_outcome=result['execution']['result']['outcome_id']
    return case


def test_second_day_real_manuscript_feedback_closes_only_original_practice(completion_ready,monkeypatch):
    case=completion_ready
    command=case.completion_command
    completed=case.client.post(f"/v1/agent-turns/{command['agent_turn_id']}/execute")
    assert completed.status_code==200,completed.text
    with case.db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',
            (case.task['plan_item_id'],)).fetchone()[0]=='completed',completed.text
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
            (case.commitment['commitment_id'],)).fetchone()[0]=='completed'
        assert c.execute('SELECT status FROM agent_objective WHERE objective_id=?',
            (case.commitment['objective_id'],)).fetchone()[0]=='paused'
        assert c.execute("SELECT status FROM agent_objective WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)",
            (case.turn['agent_turn_id'],)).fetchone()[0]=='active'
        assert c.execute("SELECT COUNT(*) FROM agent_outcome WHERE child_id=? AND outcome_level IN ('independent','transferred','retained')",
            (CHILD,)).fetchone()[0]==0
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        replay=client.post(f"/v1/agent-turns/{command['agent_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['recorded_receipt'] is True
        assert replay.json()['execution']['replay_audio_allowed'] is False
        assert_specific_feedback(case.journey.recovered(client,case.feedback_turn))
        with case.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",
                (case.task['plan_item_id'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==2
        reflection=client.post(f'/v1/sessions/{case.sid}/transition',json={
            'expected_state':'study','target_state':'reflection','reason':'Day done','client_action_id':'second-reflection'})
        assert reflection.status_code==200,reflection.text
        closed=client.post(f'/v1/sessions/{case.sid}/reflection/complete',json={
            'learned_text':'','difficult_text':'','client_action_id':'second-close'})
        assert closed.status_code==200,closed.text
        with case.db.connect() as c:
            next_obligation=c.execute("""SELECT * FROM agent_commitment WHERE child_id=?
                AND json_extract(payload_json,'$.source_outcome_id')=? AND status IN ('pending','due')""",
                (CHILD,case.followup_outcome)).fetchone()
            assert next_obligation,'The actual second feedback must remain a scheduled follow-up, not disappear at closure.'
            next_obligation=dict(next_obligation)
    case.clock.current=datetime.fromisoformat(next_obligation['due_at'])+timedelta(hours=16)
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        session=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'third-writing-day'})
        assert session.status_code==201,session.text
        sid=session.json()['session_id']
        with case.db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?",(sid,))
            c.commit()
        plan=client.post(f'/v1/sessions/{sid}/plan/generate',json={'client_action_id':'third-writing-plan'})
        assert plan.status_code==200,plan.text
        tasks=plan.json()['plan_items']
        assert any(t['source_type']=='agent_retention' and t['source_ref']==next_obligation['commitment_id'] for t in tasks),plan.text
        assert not any(t['source_ref']==case.commitment['commitment_id'] for t in tasks)
        accepted=client.post(f'/v1/sessions/{sid}/transition',json={
            'expected_state':'plan_review','target_state':'study','reason':'Accept','client_action_id':'third-writing-study'})
        assert accepted.status_code==200,accepted.text
        case.journey.gateway.control='task.start'
        case.journey.gateway.quote='开始今天的写作复习'
        voice=capture(client,case.settings,monkeypatch,sid,case.journey.gateway.quote,None,key='third-start-voice')
        turn=interpret(client,sid,case.journey.gateway.quote,'third-start',voice)
        started=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert started.status_code==200,started.text
        prompt=started.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['origin_session_id']==sid and prompt['prompt_id']!=case.completion_prompt['prompt_id']
        with case.db.connect() as c:
            link=c.execute('SELECT * FROM writing_followup WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
            assert link['parent_prompt_id']==case.completion_prompt['prompt_id']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
        assert len(case.journey.gateway.evaluations)==2


@pytest.mark.parametrize('fault',['cancel','source','goal','child','answer','plan_version'])
def test_completion_rechecks_arrival_and_original_answer_before_write(completion_ready,fault):
    case=completion_ready
    if fault=='cancel':
        stopped=case.client.post(f'/v1/sessions/{case.sid}/agent-turn',json={
            'transcript':'别讲了','client_action_id':'cancel-completion','cloud_text_allowed':False})
        assert stopped.status_code==200,stopped.text
    elif fault=='child':
        switched=case.client.post(f'/v1/sessions/{case.sid}/switch-child',json={
            'target_child_id':OTHER,'client_action_id':'completion-other-child'})
        assert switched.status_code==200,switched.text
    else:
        with case.db.connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",
                    (case.completion_prompt['diagnostic_item_id'],))
            elif fault=='goal':
                c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)",
                    (case.turn['agent_turn_id'],))
            elif fault=='answer':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer=submitted_answer||' changed' WHERE attempt_id=(SELECT attempt_id FROM diagnostic_prompt WHERE prompt_id=?)",
                    (case.completion_prompt['prompt_id'],))
            else:
                c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)',
                    (case.task['plan_item_id'],))
            c.commit()
    refused=case.client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
    assert refused.status_code==409,refused.text
    with case.db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]!='completed'
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(case.commitment['commitment_id'],)).fetchone()[0]!='completed'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",
            (case.task['plan_item_id'],)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2

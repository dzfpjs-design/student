from datetime import timedelta, datetime, timezone
import json
import base64

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService, GoalRuleConflict
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.repository import StudyRepository
from test_goal_rule_confirmation_http import request_body, authorize, changed_rules
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_goal_nanjing_calendar import freeze
from test_pet_media_evidence import wav_file

CHILD, TRACK = 'child-junior-3', 'junior_exam_readiness'
PATH = f'/v1/parent/children/{CHILD}/goal-programs/{TRACK}/rule-review'


@pytest.mark.parametrize('correct', [False, True])
def test_acknowledgement_new_day_plan_ready_but_original_plan_never_upgraded(execution, monkeypatch, correct):
    db,sid,pid,node,exact = execution
    repo = StudyRepository(db)
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        authorize(client)
        review=client.get(PATH).json()
        body=request_body(review)
        with db.connect() as c:
            original=[tuple(r) for r in c.execute('SELECT * FROM plan_constraint_decision')]
        saved=client.post(PATH,json=body)
        assert saved.status_code==200,saved.text
        assert pid in saved.json()['receipt']['superseded_plan_item_ids']
        old=GoalRoadmapService(db).resolve_execution(sid,pid)
        assert old['reason_code']=='goal_rule_reconfirmation_required'
        response=client.post(f'/v1/plan-items/{pid}/start',json={'client_action_id':'old-rule-start'})
        assert response.status_code==409,response.text
        assert response.json()['code']=='GOAL_RULE_PLAN_CHANGED'
        assert '旧安排没有自动换版' in response.json()['message']
        with db.connect() as c:
            outstanding=[r[0] for r in c.execute("SELECT plan_item_id FROM plan_item WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?) AND status='pending'",(sid,))]
        for item_id in outstanding:
            response=client.post(f'/v1/plan-items/{item_id}/reschedule',json={
                'reason':'今天先结束，按新规则另日重新安排，不算完成','client_action_id':'defer-'+item_id})
            assert response.status_code==200,response.text
        for state,target in [('plan_review','study'),('study','reflection')]:
            response=client.post(f'/v1/sessions/{sid}/transition',json=dict(expected_state=state,target_state=target,
                reason='end old day without claiming task success',client_action_id='rule-day-'+target))
            assert response.status_code==200,response.text
        repo.complete_with_reflection(sid,'','资料规则需要重排','','',True,'rule-close-original')
        with db.connect() as c:
            assert original==[tuple(r) for r in c.execute('SELECT * FROM plan_constraint_decision')]
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]=='rescheduled'
        tomorrow=datetime.now(timezone.utc)+timedelta(days=1)
        freeze(monkeypatch,tomorrow.isoformat())
        monkeypatch.setattr('home_study_agent.repository.utc_now',lambda:tomorrow.isoformat())
        new=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'rule-new-day'})
        assert new.status_code in (200,201),new.text
        new_sid=new.json()['session_id']
        for state,target in [('check_in','classroom_recall'),('classroom_recall','homework_capture')]:
            if state == 'classroom_recall':
                recorded=client.post(f'/v1/sessions/{new_sid}/recalls',json={
                    'subject':'数学','summary':'新一天的课堂记录','client_action_id':'rule-new-recall'})
                assert recorded.status_code in (200,201),recorded.text
            response=client.post(f'/v1/sessions/{new_sid}/transition',json=dict(expected_state=state,target_state=target,
                reason='new day intake',client_action_id='rule-next-'+target))
            assert response.status_code==200,response.text
        for deferred in repo.get_workspace(new_sid)['carryover_candidates']:
            response=client.post(f"/v1/sessions/{new_sid}/deferred-tasks/{deferred['source_plan_item_id']}/review",json={
                'decision':'continue_today','reason':'昨日校内作业今天保留继续，不当成已完成',
                'client_action_id':'carry-rule-'+deferred['source_plan_item_id']})
            assert response.status_code==200,response.text
        repo.add_homework(new_sid,'数学','新一天校内作业',10,'written',None,None,None,None,None,'rule-new-work')
        generated=client.post(f'/v1/sessions/{new_sid}/plan/generate',json={'client_action_id':'rule-new-plan'})
        assert generated.status_code==200,generated.text
        workspace=generated.json()
        fresh=next(i for i in workspace['plan_items'] if i['source_type']=='junior_exam_goal')
        selected=GoalRoadmapService(db).resolve_execution(new_sid,fresh['plan_item_id'])
        assert selected['status']=='ready',selected
        assert selected['diagnostic_item_id']==exact[0] and selected['binding']['knowledge_node_id']==node
        assert workspace['plan']['total_minutes']<=90
        assert client.post(f'/v1/sessions/{new_sid}/plan/generate',json={'client_action_id':'rule-new-plan'}).json()['plan']['plan_id']==workspace['plan']['plan_id']
        # Explicitly defer other work, never mark it completed merely to select the original goal.
        for item in workspace['plan_items']:
            if item['plan_item_id']!=fresh['plan_item_id'] and item['status']=='pending':
                result=client.post(f"/v1/plan-items/{item['plan_item_id']}/reschedule",json={
                    'reason':'先做目标复查，其他任务稍后安排','client_action_id':'new-defer-'+item['plan_item_id']})
                assert result.status_code==200,result.text
        entered=client.post(f'/v1/sessions/{new_sid}/transition',json=dict(expected_state='plan_review',target_state='study',
            reason='start goal recheck',client_action_id='rule-start-study'))
        assert entered.status_code==200,entered.text
        heard=client.post(f'/v1/sessions/{new_sid}/agent-turn',json={
            'transcript':'开始任务','client_action_id':'rule-fresh-voice'})
        assert heard.status_code==200,heard.text
        assert heard.json()['action']['type']=='start_next_task',heard.text
        turn_id=heard.json()['agent_turn_id']
        started=client.post(f'/v1/agent-turns/{turn_id}/execute')
        assert started.status_code==200,started.text
        prompt=started.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id']==exact[0]
    with TestClient(create_app(settings)) as client:
        authorize(client,False)
        assert client.post(PATH,json=body).json()['receipt']==saved.json()['receipt']
        assert client.get(PATH).json()['rules']['binding_status']=='current'
        assert GoalRoadmapService(Database(db.path)).resolve_execution(new_sid,fresh['plan_item_id'])['status']=='ready'
        replay=client.post(f'/v1/agent-turns/{turn_id}/execute')
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM goal_program_revision WHERE client_action_id=?',(body['client_action_id'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
        # Continue the actual Runtime journey after restart, not just prompt delivery.
        text = str(exact[1]) if correct else '-9999'
        monkeypatch.setattr(LocalMediaAI,'_recognize_wav',lambda *a,**k:(text,1000,'fixture',False))
        audio = settings.data_dir/'synthetic-roadmap-answer.wav'
        wav_file(audio)
        captured = client.post(f'/v1/sessions/{new_sid}/asr',json={
            'wav_base64':base64.b64encode(audio.read_bytes()).decode(),'purpose':'agent_command',
            'client_action_id':'rule-answer-audio','capture_reference':{'child_id':CHILD,
                'question_reference':{k:prompt[k] for k in ('prompt_id','item_version')}}})
        assert captured.status_code == 200,captured.text
        spoken = client.post(f'/v1/sessions/{new_sid}/agent-turn',json={
            'transcript':text,'voice_input_id':captured.json()['voice_input_id'],
            'client_action_id':'rule-answer-after-restart'})
        assert spoken.status_code == 200,spoken.text
        assert spoken.json()['action']['type']=='submit_diagnostic_answer',spoken.text
        # The renderer executes diagnostic.submit through the typed answer bridge,
        # not the server-only capability endpoint.
        payload = dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
            answer=spoken.json()['action']['answer'],authorized_turn_id=spoken.json()['agent_turn_id'],
            client_action_id='rule-bound-answer')
        answered = client.post(f'/v1/sessions/{new_sid}/diagnostic-prompt/answer',json=payload)
        assert answered.status_code == 200,answered.text
        with db.connect() as c:
            attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE session_id=?',(new_sid,)).fetchone()
            assert attempt is not None,answered.text
            assert attempt['diagnostic_item_id'] == exact[0]
            assert attempt['correctness'] == int(correct)
            answer_id, evidence_id = attempt['attempt_id'], attempt['evidence_id']
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]=='rescheduled'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==0
            if not correct:
                assert c.execute('SELECT 1 FROM mistake_case WHERE source_evidence_id=?',(evidence_id,)).fetchone()
                assert c.execute('SELECT 1 FROM review_task WHERE source_evidence_id=?',(evidence_id,)).fetchone()
    with TestClient(create_app(settings)) as client:
        replay = client.post(f'/v1/sessions/{new_sid}/diagnostic-prompt/answer',json=payload)
        assert replay.status_code == 200,replay.text
        with db.connect() as c:
            attempts = c.execute('SELECT attempt_id,evidence_id FROM diagnostic_attempt WHERE session_id=?',(new_sid,)).fetchall()
            assert [tuple(a) for a in attempts] == [(answer_id,evidence_id)]


def test_source_swap_inside_confirmation_transaction_rolls_back(goal_db,monkeypatch):
    service=GoalProgramService(goal_db)
    service.seed_defaults()
    review=service.review_rules(CHILD,TRACK)
    from home_study_agent import official_goal_rules
    original=official_goal_rules.rule_snapshot
    calls=[]
    def source(track):
        snapshot=original(track)
        calls.append(track)
        if len(calls)==2:
            snapshot={**snapshot,'snapshot_hash':'f'*64}
        return snapshot
    monkeypatch.setattr(official_goal_rules,'rule_snapshot',source)
    with goal_db.connect() as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM goal_program')]
    with pytest.raises(GoalRuleConflict,match='核对期间规则已变化'):
        service.confirm_rules(CHILD,TRACK,**request_body(review))
    with goal_db.connect() as c:
        assert before==[tuple(r) for r in c.execute('SELECT * FROM goal_program')]
        assert c.execute('SELECT COUNT(*) FROM goal_program_revision').fetchone()[0]==0


def test_revision_insert_failure_rolls_back_and_samekey_recovers(goal_db):
    import sqlite3
    service=GoalProgramService(goal_db)
    service.seed_defaults()
    before=service.review_rules(CHILD,TRACK)
    with goal_db.connect() as c:
        c.execute("CREATE TRIGGER reject_rule BEFORE INSERT ON goal_program_revision BEGIN SELECT RAISE(ABORT,'isolated_storage_fault'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='isolated_storage_fault'):
        service.confirm_rules(CHILD,TRACK,**request_body(before))
    assert service.review_rules(CHILD,TRACK)==before
    with goal_db.connect() as c:
        c.execute('DROP TRIGGER reject_rule');c.commit()
    saved=service.confirm_rules(CHILD,TRACK,**request_body(before))
    assert service.confirm_rules(CHILD,TRACK,**request_body(before))['receipt']==saved['receipt']

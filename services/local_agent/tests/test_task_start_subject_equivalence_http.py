"""Actual Runtime/HTTP/DB; only synthetic ASR and planner inference are controlled."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.repository import StudyRepository
from home_study_agent.task_start_binding import TaskStartBinding
from test_diagnostic_prompt_http import environment,start
from test_cloud_first_agent import Planner
from test_voice_runtime_binding import capture,interpret


HOMEWORK='数学练习是做家长选好的三道原创合成练习。'
START='现在开始数学练习。'

def say(client,settings,monkeypatch,gateway,sid,quote,key,cap,args):
 gateway.payload=Planner(cap,args,quote).payload
 voice=capture(client,settings,monkeypatch,sid,quote,None,key+'-voice')
 turn=interpret(client,sid,quote,key,voice)
 return turn

def execute(client,turn):
 response=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
 assert response.status_code==200,response.text
 return response.json()

def make_plan(client,settings,db,monkeypatch,gateway):
 sid=start(client)
 repo=StudyRepository(db)
 repo.save_planning_context(sid,'medium',0,None,None,None,0,'public-context')
 repo.transition(sid,'check_in','classroom_recall','test','public-recall')
 repo.declare_empty_stage(sid,'no_recall','public-no-recall')
 repo.transition(sid,'classroom_recall','homework_capture','test','public-homework')
 add=say(client,settings,monkeypatch,gateway,sid,HOMEWORK,'public-add','homework.add',{'subject':'math','title':'家长选好的三道原创合成练习','expected_minutes':20,'task_type':'written'})
 assert add['runtime_capability']=='homework.add';execute(client,add)
 plan=say(client,settings,monkeypatch,gateway,sid,'作业说完了，帮我安排计划。','public-plan','planning.generate',{})
 assert plan['runtime_capability']=='planning.generate';execute(client,plan)
 confirm=say(client,settings,monkeypatch,gateway,sid,'这个计划可以。','public-confirm','session.transition',{'target':'study'})
 assert confirm['runtime_capability']=='session.transition';execute(client,confirm)
 with db.connect()as c:
  assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]=='study'
  task=c.execute("SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE p.session_id=? AND i.source_type='school_homework'",(sid,)).fetchone()
  assert task['subject']=='math'and task['status']=='pending'
 return sid,dict(task)

def planned_start(client,settings,monkeypatch,gateway,sid,key='public-start'):
 return say(client,settings,monkeypatch,gateway,sid,START,key,'task.start',{'start_selector':{'subject':'math','task_kind':'written','source_type':'school_homework','target_quote':START}})

def test_actual_http_math_homework_confirm_and_chinese_start_preserves_stored_code(environment,monkeypatch):
 settings,db=environment;gateway=Planner('conversation.respond',{'response':'公开合成'},'公开合成')
 with TestClient(create_app(settings,model_gateway=gateway))as client:
  sid,item=make_plan(client,settings,db,monkeypatch,gateway)
  turn=planned_start(client,settings,monkeypatch,gateway,sid)
  assert turn['runtime_capability']=='task.start',('CANONICAL_SUBJECT_CODE_WAS_NOT_MATCHED',turn['action']['type'])
  execute(client,turn)
  with db.connect()as c:
   current=c.execute('SELECT subject,status,source_ref,homework_id FROM plan_item WHERE plan_item_id=?',(item['plan_item_id'],)).fetchone()
   assert current['subject']=='math'and current['status']=='active'and current['source_ref']==item['source_ref']and current['homework_id']==item['homework_id']
   assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(item['plan_item_id'],)).fetchone()[0]==1
   assert c.execute('SELECT subject FROM homework_item WHERE homework_id=?',(item['homework_id'],)).fetchone()[0]=='math'

@pytest.mark.parametrize('case,expected',[('missing','requested_task_missing'),('unknown_alias','requested_task_missing'),('conflict','requested_task_ambiguous'),('duplicate','requested_task_ambiguous'),('cross_child','no_task')])
def test_closed_alias_keeps_existing_target_boundaries(environment,monkeypatch,case,expected):
 settings,db=environment;gateway=Planner('conversation.respond',{'response':'公开合成'},'公开合成')
 with TestClient(create_app(settings,model_gateway=gateway))as client:
  sid,item=make_plan(client,settings,db,monkeypatch,gateway)
  with db.connect()as c:
   if case=='unknown_alias':c.execute("UPDATE plan_item SET subject='mathematics-near-name' WHERE plan_item_id=?",(item['plan_item_id'],))
   if case=='duplicate':
    c.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type) VALUES('another',?,'数学','公开第二项',10,5,'pending',5,'school_homework','another','written')",(item['plan_id'],))
   snap=TaskStartBinding.start_snapshot(c,sid,'child-junior-3'if case=='cross_child'else'child-primary-4')
   words='开始物理作业'if case=='missing'else'开始数学和英语作业'if case=='conflict'else START
   result=TaskStartBinding.select_start_target(snap,TaskStartBinding.requested_start_filter(words))
   assert result['status']=='blocked'and result['reason']==expected and result['item']is None


def test_semantically_equal_code_change_after_original_arrival_still_rejects(environment,monkeypatch):
 settings,db=environment;gateway=Planner('conversation.respond',{'response':'公开合成'},'公开合成')
 with TestClient(create_app(settings,model_gateway=gateway))as client:
  sid,item=make_plan(client,settings,db,monkeypatch,gateway)
  turn=planned_start(client,settings,monkeypatch,gateway,sid)
  assert turn['runtime_capability']=='task.start'
  with db.connect()as c:c.execute("UPDATE plan_item SET subject='数学' WHERE plan_item_id=?",(item['plan_item_id'],));c.commit()
  response=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
  assert response.status_code==409
  with db.connect()as c:assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0]==0

"""Real HTTP/Database: withdrawn experience needs rereview, never old approval."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_parent_historical_action_feedback import seed, checked, CHILD, OTHER

EDIT={'coaching_decision':'custom','coaching_script':'公开合成：先听你说一步。','coaching_action':'公开合成：只核对一步。',
 'coaching_duration_minutes':2,'parent_note':'公开合成：保留已写内容','coaching_observation':'公开合成：不作掌握判断'}

def arranged(client):
 seed(client)
 h={'X-HSC-Parent':checked(client,'POST','/v1/parent/setup',{'pin':'2468'})['parent_token']}
 weekly=checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=weekly',headers=h)
 rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
 weekly=next(x for x in rows if x['report_type']=='weekly' and x['status']=='draft')
 w='/v1/parent/report-snapshots/'+weekly['snapshot_id']
 checked(client,'POST',w+'/approve',{},h)
 checked(client,'POST',w+'/feedback',{'helpfulness':4,'stress_rating':2,'notes':'公开合成主观体验','client_action_id':'carry-experience'},h)
 monthly=checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=monthly',headers=h)
 rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
 old=next(x for x in rows if x['report_type']=='monthly' and x['status']=='draft')
 oldpath='/v1/parent/report-snapshots/'+old['snapshot_id']
 saved=checked(client,'PUT',oldpath,EDIT,h)
 checked(client,'POST',f'/v1/parent/children/{CHILD}/report-snapshots/{weekly["snapshot_id"]}/feedback-retractions',
  {'expected_revision':1,'client_action_id':'carry-withdraw-experience','reason':'公开合成：撤回原主观体验'},h)
 # The actual original period refresh method runs in a worker with its own DB connections.
 def refresh():
  result=client.app.state.period_reports.refresh_current(CHILD,require_evidence=False)
  assert not result['failures']
  for period in result['reports']: client.app.state.parent_reports.create_from_period(period)
 with ThreadPoolExecutor(max_workers=1) as worker:worker.submit(refresh).result()
 old=checked(client,'GET',oldpath,headers=h)
 assert old['status']=='revoked' and old['correction_status']['source_current'] is False
 assert old['parent_edit']==saved['parent_edit']
 checked(client,'POST',oldpath+'/approve',{},h,status=409)
 target=checked(client,'GET','/v1/parent/report-snapshots/'+old['correction_status']['latest_replacement_id'],headers=h)
 assert target['status']=='draft' and target['correction_status']['source_current'] is True
 assert target['source_id'].rpartition('@')[0]==old['source_id'].rpartition('@')[0]
 request={'source_snapshot_id':old['snapshot_id'],'source_edit_version':old['parent_edit_version'],
  'target_edit_version':target['parent_edit_version'],'client_action_id':'carry-same-request-once'}
 return h,old,target,request

def settings(tmp_path):
 return Settings(data_dir=tmp_path,database_path=tmp_path/'public.db',maintenance_enabled=False,
  model_api_key=None,fallback_model_api_key=None,cloud_asr_provider='disabled',tts_provider='disabled')

def test_withdrawn_experience_carry_is_explicit_atomic_replayable_and_reopens(tmp_path):
 cfg=settings(tmp_path)
 with TestClient(create_app(cfg)) as client:
  h,old,target,payload=arranged(client)
  path='/v1/parent/report-snapshots/'+target['snapshot_id']
  before={k:copy.deepcopy(target[k]) for k in ('facts','inferences','content','confidence','evidence_version','source_version','status')}
  result=checked(client,'POST',path+'/carry-saved-edits',payload,h)
  assert result['parent_edit']==old['parent_edit'] and result['status']=='draft'
  assert {k:result[k] for k in before}==before
  assert result['edit_transfer']['replayed'] is False
  # A lost response is recovered by the same command; no second audit/write.
  again=checked(client,'POST',path+'/carry-saved-edits',payload,h)
  assert again['edit_transfer']['replayed'] is True and again['parent_edit']==result['parent_edit']
  checked(client,'POST',path+'/carry-saved-edits',{**payload,'source_snapshot_id':target['snapshot_id']},h,status=409)
  # Fresh approval is separate and freezes current evidence with carried human edits.
  approved=checked(client,'POST',path+'/approve',{},h)
  assert approved['status']=='approved' and approved['parent_action']['binding_status']=='bound'
  assert approved['parent_action']['review_binding']['decision']=='custom'
  assert approved['parent_action']['coaching']['action']['what']==EDIT['coaching_action']
  ids=(old['snapshot_id'],target['snapshot_id'])
 with TestClient(create_app(cfg)) as reopened:
  h={'X-HSC-Parent':checked(reopened,'POST','/v1/parent/unlock',{'pin':'2468'})['parent_token']}
  old2=checked(reopened,'GET','/v1/parent/report-snapshots/'+ids[0],headers=h)
  new2=checked(reopened,'GET','/v1/parent/report-snapshots/'+ids[1],headers=h)
  assert old2['parent_edit']==new2['parent_edit']==result['parent_edit']
  assert new2['status']=='approved' and new2['parent_action']['coaching']==approved['parent_action']['coaching']
 with Database(cfg.database_path).connect() as c:
  assert c.execute("SELECT count(*) FROM audit_event WHERE action='parent_report_saved_edits_carried'").fetchone()[0]==1

def test_carry_compare_and_swap_scope_revoke_and_source_changes_fail_closed(tmp_path):
 with TestClient(create_app(settings(tmp_path))) as client:
  h,old,target,payload=arranged(client);path='/v1/parent/report-snapshots/'+target['snapshot_id']
  checked(client,'POST',path+'/carry-saved-edits',payload,status=401)
  checked(client,'POST',path+'/carry-saved-edits',{**payload,'source_edit_version':'0'*64},h,status=409)
  checked(client,'POST',path+'/carry-saved-edits',{**payload,'target_edit_version':'0'*64},h,status=409)
  checked(client,'POST',path+'/carry-saved-edits',{**payload,'extra':True},h,status=422)
  changed=checked(client,'PUT',path,{'parent_note':'公开合成：另一审核窗口已修改'},h)
  checked(client,'POST',path+'/carry-saved-edits',payload,h,status=409)
  assert checked(client,'GET',path,headers=h)['parent_edit']==changed['parent_edit']
  # Explicit parent withdrawal cannot be converted to a transfer permission.
  checked(client,'POST','/v1/parent/report-snapshots/'+old['snapshot_id']+'/revoke',{'reason':'公开合成：家长明确撤销'},h)
  payload={**payload,'target_edit_version':changed['parent_edit_version']}
  checked(client,'POST',path+'/carry-saved-edits',payload,h,status=409)
  assert checked(client,'GET',path,headers=h)['status']=='draft'

def test_carry_rejects_other_child_period_and_new_edits_then_serializes_competing_commands(tmp_path):
 from datetime import date,timedelta
 with TestClient(create_app(settings(tmp_path))) as client:
  h,old,target,payload=arranged(client);parents=client.app.state.parent_reports
  def route(row):return '/v1/parent/report-snapshots/'+row['snapshot_id']+'/carry-saved-edits'
  # Distinct live period identities, including another month of the same child.
  other=client.app.state.period_reports.generate(OTHER,'monthly')
  other=parents.create_from_period(other)
  before_month=date.today().replace(day=1)-timedelta(days=1)
  older=parents.create_from_period(client.app.state.period_reports.generate(CHILD,'monthly',before_month))
  weekly=next(x for x in parents.list(CHILD) if x['report_type']=='weekly' and x['status']=='draft')
  for row in (other,older,weekly):
   checked(client,'POST',route(row),{**payload,'target_edit_version':row['parent_edit_version']},h,status=409)
  checked(client,'POST',route(target),{**payload,'source_snapshot_id':target['snapshot_id']},h,status=409)
  # Two actual authenticated requests race on the same saved target version.
  def apply(key):return client.post(route(target),json={**payload,'client_action_id':key},headers=h)
  with ThreadPoolExecutor(max_workers=2) as worker:
   responses=list(worker.map(apply,('carry-racer-one','carry-racer-two')))
  assert sorted(r.status_code for r in responses)==[200,409]
  assert next(r for r in responses if r.status_code==409).json()['detail']['code']=='REPORT_EDIT_CARRY_NOT_APPLIED'
  got=checked(client,'GET','/v1/parent/report-snapshots/'+target['snapshot_id'],headers=h)
  assert got['status']=='draft' and got['parent_edit']==old['parent_edit']
  winner=next(r for r in responses if r.status_code==200).json()['edit_transfer']
  # Later user edits remain intact even when an earlier successful request is reread.
  changed=checked(client,'PUT','/v1/parent/report-snapshots/'+target['snapshot_id'],{'parent_note':'公开合成：成功转接后的新人工修改'},h)
  replay=checked(client,'POST',route(target),{**payload,'client_action_id':winner['client_action_id']},h)
  assert replay['edit_transfer']['replayed'] and replay['parent_edit']==changed['parent_edit']
  with Database(client.app.state.parent_reports.database.path).connect() as c:
   assert c.execute("SELECT count(*) FROM audit_event WHERE action='parent_report_saved_edits_carried'").fetchone()[0]==1

def test_untouched_adopt_null_duration_is_carried_without_granting_approval(tmp_path):
 # This stored value is a legal untouched draft, not a new nullable edit request.
 import home_study_agent.parent_reports as module
 with TestClient(create_app(settings(tmp_path))) as client:
  h,old,target,payload=arranged(client)
  with client.app.state.parent_reports.database.connect() as c:
   defaults=module.ParentReportService._normalized_parent_edit(None)
   c.execute('UPDATE parent_report_snapshot SET parent_edit_json=? WHERE snapshot_id=?',(json.dumps(defaults),old['snapshot_id']));c.commit()
  source=checked(client,'GET','/v1/parent/report-snapshots/'+old['snapshot_id'],headers=h)
  result=checked(client,'POST','/v1/parent/report-snapshots/'+target['snapshot_id']+'/carry-saved-edits',
   {**payload,'source_edit_version':source['parent_edit_version']},h)
  assert result['parent_edit']==defaults and result['status']=='draft'

def test_carry_requires_original_system_supersession_witness_and_live_source(tmp_path):
 with TestClient(create_app(settings(tmp_path))) as client:
  h,old,target,payload=arranged(client);path='/v1/parent/report-snapshots/'+target['snapshot_id']+'/carry-saved-edits'
  db=client.app.state.parent_reports.database
  with db.connect() as c:
   events=c.execute("SELECT * FROM audit_event WHERE action='parent_period_snapshot_superseded' AND child_id=? AND json_extract(summary_json,'$.snapshot_id')=?",(CHILD,old['snapshot_id'])).fetchall()
   assert events
  for field,bad in [('actor','parent'),('action','synthetic_not_supersession'),('occurred_at','2000-01-01T00:00:00+00:00')]:
   with db.connect() as c:
    for row in events:c.execute('UPDATE audit_event SET '+field+'=? WHERE audit_id=?',(bad,row['audit_id']))
    c.commit()
   result=checked(client,'POST',path,payload,h,status=409)
   assert result['detail']['code']=='REPORT_EDIT_CARRY_NOT_APPLIED'
   with db.connect() as c:
    for row in events:c.execute('UPDATE audit_event SET '+field+'=? WHERE audit_id=?',(row[field],row['audit_id']))
    c.commit()
  with db.connect() as c:
   row=c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',(target['source_id'].rpartition('@')[0],)).fetchone()
   content=json.loads(row['content_json']);content['learning_source_fingerprint']='0'*64
   c.execute('UPDATE period_report SET content_json=? WHERE period_report_id=?',(json.dumps(content),target['source_id'].rpartition('@')[0]));c.commit()
  checked(client,'POST',path,payload,h,status=409)
  with db.connect() as c:
   assert c.execute("SELECT count(*) FROM audit_event WHERE action='parent_report_saved_edits_carried'").fetchone()[0]==0
   saved=c.execute('SELECT parent_edit_json,status FROM parent_report_snapshot WHERE snapshot_id=?',(target['snapshot_id'],)).fetchone()
   assert client.app.state.parent_reports._normalized_parent_edit(saved['parent_edit_json'])==target['parent_edit'] and saved['status']=='draft'


def test_stale_self_is_not_offered_as_newer_draft_and_normal_refresh_builds_exact_replacement(tmp_path):
 with TestClient(create_app(settings(tmp_path))) as client:
  seed(client)
  h={'X-HSC-Parent':checked(client,'POST','/v1/parent/setup',{'pin':'2468'})['parent_token']}
  checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=weekly',headers=h)
  weekly=next(x for x in checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items'] if x['report_type']=='weekly' and x['status']=='draft')
  w='/v1/parent/report-snapshots/'+weekly['snapshot_id'];checked(client,'POST',w+'/approve',{},h)
  checked(client,'POST',w+'/feedback',{'helpfulness':4,'stress_rating':2,'notes':'公开合成主观体验','client_action_id':'stale-self-experience'},h)
  checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=monthly',headers=h)
  old=next(x for x in checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items'] if x['report_type']=='monthly' and x['status']=='draft')
  path='/v1/parent/report-snapshots/'+old['snapshot_id'];saved=checked(client,'PUT',path,EDIT,h)
  checked(client,'POST',f'/v1/parent/children/{CHILD}/report-snapshots/{weekly["snapshot_id"]}/feedback-retractions',{'expected_revision':1,'client_action_id':'stale-self-withdraw-experience','reason':'公开合成：撤回主观体验'},h)
  # Real refresh worker persists current period content, deliberately stops before
  # the caller materializes reviewed snapshots. No original failed DB is used.
  with ThreadPoolExecutor(max_workers=1)as worker:
   refreshed=worker.submit(client.app.state.period_reports.refresh_current,CHILD,False).result()
  assert not refreshed['failures'] and any(p['report_type']=='monthly'for p in refreshed['reports'])
  before=checked(client,'GET',path,headers=h)
  assert before['status']=='draft' and before['parent_edit']==saved['parent_edit']
  assert before['correction_status']['source_current']is False
  assert before['correction_status']['latest_replacement_id']!=before['snapshot_id'], 'STALE_SELF_OFFERED_AS_REPLACEMENT'
  assert before['correction_status']['latest_replacement_availability']=='none'
  checked(client,'POST',path+'/approve',{},h,status=409)
  # This is exactly the normal UI viewReplacementWithSavedEdits first API call.
  checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)
  old2=checked(client,'GET',path,headers=h);nid=old2['correction_status']['latest_replacement_id']
  assert nid and nid!=old['snapshot_id'] and old2['status']=='revoked'
  target=checked(client,'GET','/v1/parent/report-snapshots/'+nid,headers=h)
  assert target['child_id']==old['child_id'] and target['source_kind']==old['source_kind'] and target['report_type']=='monthly'
  assert target['source_id'].rpartition('@')[0]==old['source_id'].rpartition('@')[0]
  assert target['content']['report_date']==old['content']['report_date'] and target['status']=='draft' and target['correction_status']['source_current']is True
  assert old2['parent_edit']==saved['parent_edit'] and target['parent_edit']!=saved['parent_edit']
  receipt=checked(client,'POST','/v1/parent/report-snapshots/'+nid+'/carry-saved-edits',{'source_snapshot_id':old['snapshot_id'],'source_edit_version':old2['parent_edit_version'],'target_edit_version':target['parent_edit_version'],'client_action_id':'stale-self-explicit-carry'},h)
  assert receipt['status']=='draft' and receipt['parent_edit']==saved['parent_edit']
  assert receipt['facts']==target['facts'] and receipt['content']==target['content']
  approved=checked(client,'POST','/v1/parent/report-snapshots/'+nid+'/approve',{},h)
  assert approved['status']=='approved' and approved['parent_action']['review_binding']['decision']=='custom'

"""Public synthetic actual HTTP: a legitimate reissued approval retains feedback authority only."""
import copy,hashlib,json
from datetime import datetime,timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from test_parent_report_source_reissue import arrange_cycle,settings,route,EDIT_B
from test_parent_historical_action_feedback import checked,CHILD,OTHER,payload

def approved_reissue(client):
    h,a,b,_,rows=arrange_cycle(client)
    fresh=next(x for x in rows if x['report_type']=='monthly'and x['status']=='draft'and x['correction_status']['source_current'])
    fresh=checked(client,'PUT',route(fresh),EDIT_B,h)
    approved=checked(client,'POST',route(fresh)+'/approve',{},h)
    assert approved['snapshot_id']not in(a['snapshot_id'],b['snapshot_id'])and approved['source_id']not in(a['source_id'],b['source_id'])
    assert approved['status']=='approved'and approved['parent_action']['binding_status']=='bound'
    return h,a,b,approved,approved['parent_action']

def test_http_reissued_approval_feedback_reopen_replay_and_withdrawal(tmp_path):
    cfg=settings(tmp_path)
    with TestClient(create_app(cfg))as client:
        h,a,b,review,action=approved_reissue(client)
        url=f"/v1/parent/children/{CHILD}/parent-actions/{action['snapshot_id']}"
        request=payload(action,'reissued-action-once')
        result=client.put(url+'/feedback',headers=h,json=request)
        assert result.status_code==200,('REISSUED_APPROVAL_FEEDBACK_REJECTED',result.status_code,result.text)
        receipt=result.json();assert receipt['action_snapshot_id']==action['snapshot_id']and receipt['revision']==1
        eligibility=checked(client,'GET',url,headers=h)['feedback_eligibility']
        assert eligibility['record_allowed']and eligibility['approval_witness_valid']and eligibility['current_execution_granted']is False
        checked(client,'GET',url.replace(CHILD,OTHER),headers=h,status=404)
        checked(client,'PUT',url.replace(CHILD,OTHER)+'/feedback',request,h,status=404)
        checked(client,'PUT',url+'/feedback',{**request,'action_snapshot_id':'foreign-action'},h,status=409)
        checked(client,'PUT',url+'/feedback',{**request,'client_action_id':'stale-revision'},h,status=409)
        # Later real versions must not replace the predecessor used at issuance.
        checked(client,'POST',route(review)+'/feedback',{'helpfulness':2,'stress_rating':3,
            'notes':'公开合成：后续报告体验变更','client_action_id':'later-report-experience'},h)
        checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=monthly',headers=h)
        checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)
        historical=checked(client,'GET',route(review),headers=h);assert historical['status']=='revoked'
        eligibility=checked(client,'GET',url,headers=h)['feedback_eligibility']
        assert eligibility=={'record_allowed':True,'approval_witness_valid':True,'current_execution_granted':False,'reason':'system_superseded_approval'}
        service=client.app.state.parent_reports
        actions=ParentActionEvidenceService(service.database)
        with service.database.connect()as c:
            frozen=json.loads(c.execute('SELECT payload_json FROM parent_action_snapshot WHERE snapshot_id=?',(action['snapshot_id'],)).fetchone()[0])
            assert actions._review_current(c,frozen)is False
            assert not any(x['action_snapshot_id']==action['snapshot_id']for x in actions.feedback_for_child(c,CHILD))
    with TestClient(create_app(cfg))as reopened:
        h={'X-HSC-Parent':checked(reopened,'POST','/v1/parent/unlock',{'pin':'2468'})['parent_token']}
        assert checked(reopened,'GET',url+'/history',headers=h)['items']==[receipt]
        assert checked(reopened,'PUT',url+'/feedback',request,h)==receipt
        checked(reopened,'POST',route(review)+'/revoke',{'reason':'公开合成：家长明确撤销新签报告'},h)
        checked(reopened,'PUT',url+'/feedback',{**request,'expected_revision':1,'client_action_id':'denied-after-withdrawal'},h,status=409)
        undone=checked(reopened,'PUT',url+'/feedback',{'action_snapshot_id':action['snapshot_id'],'expected_revision':1,
            'operation':'retract','reason':'公开合成撤回','client_action_id':'retract-original-reissued-feedback'},h)
        assert undone['operation']=='retract'and undone['revision']==2

def test_reissue_identity_requires_original_content_and_authenticated_predecessor_graph(tmp_path):
    with TestClient(create_app(settings(tmp_path)))as client:
        h,a,b,review,action=approved_reissue(client)
        db=client.app.state.parent_reports.database;service=ParentActionEvidenceService(db)
        with db.connect()as c:
            row=c.execute('SELECT * FROM parent_action_snapshot WHERE snapshot_id=?',(action['snapshot_id'],)).fetchone();frozen=json.loads(row['payload_json'])
            assert service._historical_feedback_eligibility(c,row,frozen)['record_allowed']
            origin=c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(a['snapshot_id'],)).fetchone()
            root=review['source_id'].rpartition('@')[0];base_id=a['source_id']
            edge=c.execute("SELECT * FROM audit_event WHERE actor='system'AND action='parent_period_snapshot_superseded'AND json_extract(summary_json,'$.snapshot_id')=?AND json_extract(summary_json,'$.replacement_snapshot_id')=?",(a['snapshot_id'],b['snapshot_id'])).fetchone();assert edge
            def deny(label,mutation,changed=None):
                c.execute('SAVEPOINT witness_case')
                try:
                    mutation();value=copy.deepcopy(frozen)
                    if changed:changed(value)
                    edited={**dict(row),'fingerprint':hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()}
                    result=service._historical_feedback_eligibility(c,edited,value)
                    assert result['record_allowed']is False and result['current_execution_granted']is False,label
                finally:c.execute('ROLLBACK TO witness_case');c.execute('RELEASE witness_case')
            for code in ('0'*16,hashlib.sha256(json.dumps({'schema':'period_reissue_v1','content_source_id':base_id,'system_corrective_predecessor':a['snapshot_id']},sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]):
                source=review['source_version'].rsplit(':',1)[0]+':'+code
                deny('fake-or-wrong-predecessor-issuance',lambda code=code,source=source:c.execute('UPDATE parent_report_snapshot SET source_id=?,source_version=?WHERE snapshot_id=?',(root+'@'+code,source,review['snapshot_id'])),lambda v,source=source:v['review_binding'].update(source_version=source))
            deny('missing-system-edge',lambda:c.execute('DELETE FROM audit_event WHERE audit_id=?',(edge['audit_id'],)))
            deny('parent-text-is-not-system-edge',lambda:c.execute("UPDATE audit_event SET actor='parent'WHERE audit_id=?",(edge['audit_id'],)))
            deny('cross-child-system-edge',lambda:c.execute('UPDATE audit_event SET child_id=?WHERE audit_id=?',(OTHER,edge['audit_id'])))
            deny('cross-root-content-origin',lambda:c.execute('UPDATE parent_report_snapshot SET source_id=?WHERE snapshot_id=?',('foreign-root@'+base_id.rpartition('@')[2],a['snapshot_id'])))
            bad=json.loads(origin['content_json']);bad['headline']='公开合成：非原内容'
            deny('changed-original-content',lambda:c.execute('UPDATE parent_report_snapshot SET content_json=?WHERE snapshot_id=?',(json.dumps(bad,ensure_ascii=False),a['snapshot_id'])))
            deny('changed-original-evidence',lambda:c.execute('UPDATE parent_report_snapshot SET evidence_version=?WHERE snapshot_id=?',('sha256:'+'0'*64,a['snapshot_id'])))
            def invalid_time():
                c.execute('UPDATE audit_event SET occurred_at=?WHERE audit_id=?',('1900-01-01T00:00:00+00:00',edge['audit_id']))
                c.execute('UPDATE parent_report_snapshot SET revoked_at=?WHERE snapshot_id=?',('1900-01-01T00:00:00+00:00',a['snapshot_id']))
            deny('system-edge-time-before-source',invalid_time)
            def prior_parent_withdrawal():
                c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)VALUES(?,'parent','parent_report_snapshot_revoked',?,?,?)",(str(uuid4()),CHILD,json.dumps({'snapshot_id':a['snapshot_id']}),origin['created_at']))
            deny('same-content-explicitly-withdrawn-at-issuance',prior_parent_withdrawal)

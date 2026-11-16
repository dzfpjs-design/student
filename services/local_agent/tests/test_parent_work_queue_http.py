from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.parent_auth import ParentAuth
from home_study_agent.parent_work_queue import ParentWorkQueueService
from home_study_agent.parent_work_queue_http import create_parent_work_queue_router
from home_study_agent.database import Database
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from test_parent_work_queue import work, ocr_items, CHILD, OTHER


def host(db, auth):
    app = FastAPI()
    app.include_router(create_parent_work_queue_router(ParentWorkQueueService(db), auth))
    return TestClient(app, client=('127.0.0.1', 50200))


def test_real_auth_scope_pagination_restart_and_malformed_cursor(work):
    db, _, _, ocr, _ = work
    rows = ocr_items(ocr, CHILD, 35)
    auth = ParentAuth(db)
    token = auth.setup('7391')
    client = host(db, auth)
    root='/v1/parent/work-queue'
    assert client.get(root).status_code==401
    client.headers['X-HSC-Parent']=token
    first=client.get(root,params={'child_id':CHILD,'limit':7})
    assert first.status_code==200 and first.headers['Cache-Control']=='no-store'
    page=first.json()
    assert page['total']==35
    cursor=page['next_cursor']
    for params in ({'child_id':OTHER,'limit':7,'cursor':cursor}, {'child_id':CHILD,'limit':8,'cursor':cursor}):
        assert client.get(root,params=params).status_code==409
    for bad in ('!', 'not:base64', '====', 'e30=', 'bnVsbA==', 'W10='):
        assert client.get(root,params={'cursor':bad}).status_code==422
    assert client.get(root,params={'child_id':'deleted'}).status_code==404
    assert client.get(root,params={'actor':'parent'}).status_code==422
    parent_view=client.get(root,params={'child_id':CHILD,'audience':'parent_decisions'}).json()
    assert parent_view['total']==0 and parent_view['system_verification_total']>=35
    assert {source['kind'] for source in parent_view['sources']}=={'safety','delivery_unknown'}
    assert client.get(root,params={'audience':'developer'}).status_code==422
    assert client.post(root,json={}).status_code==405
    ocr.batch([rows[0]['review_id']],CHILD,'skip','skip-http-existing-service')
    assert client.get(root,params={'child_id':CHILD,'limit':7,'cursor':cursor}).status_code==409
    reopened=Database(db.path)
    newauth=ParentAuth(reopened)
    restarted=host(reopened,newauth)
    restarted.headers['X-HSC-Parent']=token
    assert restarted.get(root).status_code==401
    restarted.headers['X-HSC-Parent']=newauth.unlock('7391')
    assert restarted.get(root,params={'child_id':CHILD}).json()['total']==34
    assert restarted.get('/v1/parent/children/'+OTHER+'/work-supply-gaps/missing').status_code==404


def test_authenticated_nonlocal_request_is_forbidden(work):
    db,*_=work
    auth=ParentAuth(db)
    token=auth.setup('7391')
    app=FastAPI()
    app.include_router(create_parent_work_queue_router(ParentWorkQueueService(db),auth))
    client=TestClient(app,client=('203.0.113.2',50000),headers={'X-HSC-Parent':token})
    assert client.get('/v1/parent/work-queue').status_code==403


def test_blocked_job_detail_actual_http_auth_scope_and_no_mutation(work):
    db,_,_,_,_=work
    supply=SpecializedDiagnosticSupplyService(db)
    job=supply.enqueue_local('diag-pet-writing-1',CHILD,'http-local-blocked')
    supply.process_local_pending()
    auth=ParentAuth(db)
    token=auth.setup('7391')
    client=host(db,auth)
    root=f"/v1/parent/children/{CHILD}/work-supply-gaps/{job['job_id']}"
    assert client.get(root).status_code==401
    client.headers['X-HSC-Parent']=token
    before=supply.list(CHILD)
    result=client.get(root)
    assert result.status_code==200 and result.headers['Cache-Control']=='no-store'
    assert result.json()['status']=='blocked' and result.json()['job_id']==job['job_id']
    assert result.json()['evidence'][0]['text']
    assert client.get(root.replace(CHILD,OTHER)).status_code==404
    assert client.get('/v1/parent/work-queue?child_id='+CHILD+'&child_id='+OTHER).status_code==422
    assert supply.list(CHILD)==before

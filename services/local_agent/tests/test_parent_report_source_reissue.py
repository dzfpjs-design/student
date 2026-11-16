"""Real public-synthetic HTTP A -> B -> A. No family/private-copy DB writes."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_parent_historical_action_feedback import seed, checked, CHILD

EDIT_A={'coaching_decision':'custom','coaching_script':'公开合成：原A已审修改',
        'coaching_action':'公开合成：原A一步','coaching_duration_minutes':2,
        'parent_note':'公开合成：保留原A历史'}
EDIT_B={**EDIT_A,'coaching_script':'公开合成：B已存修改','parent_note':'公开合成：保留B人工劳动'}

def settings(tmp_path):
    return Settings(data_dir=tmp_path,database_path=tmp_path/'public-cycle.db',
        maintenance_enabled=False,model_api_key=None,fallback_model_api_key=None,
        cloud_asr_provider='disabled',tts_provider='disabled')

def route(row): return '/v1/parent/report-snapshots/'+row['snapshot_id']

def arrange_cycle(client, explicit_parent_revoke=False, delivery_state=None, finish_list=True):
    seed(client)
    h={'X-HSC-Parent':checked(client,'POST','/v1/parent/setup',{'pin':'2468'})['parent_token']}
    for kind in ('weekly','monthly'):
        checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type={kind}',headers=h)
    rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
    weekly=next(x for x in rows if x['report_type']=='weekly' and x['status']=='draft')
    weekly=checked(client,'POST',route(weekly)+'/approve',{},h)
    rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
    a=next(x for x in rows if x['report_type']=='monthly' and x['status']=='draft'
           and x['correction_status']['source_current'] is True)
    a=checked(client,'PUT',route(a),EDIT_A,h)
    a=checked(client,'POST',route(a)+'/approve',{},h)
    assert a['status']=='approved'
    if delivery_state:
        recipient=checked(client,'POST','/v1/parent/report-delivery/recipients',
            {'child_id':CHILD,'channel':'email','address':'synthetic@localhost',
             'label':'公开合成本机收件人','consented':True},h)
        delivery=checked(client,'POST',route(a)+'/deliveries',
            {'recipient_id':recipient['recipient_id'],'scheduled_at':'2099-01-01T00:00:00+00:00'},h)
        if delivery_state=='unknown':
            # Public synthetic crash fixture only: no transport/send is invoked.
            # Original initialization recovers an interrupted submission to unknown.
            db=client.app.state.parent_reports.database
            with db.connect() as c:
                c.execute("UPDATE report_delivery_outbox SET status='processing' WHERE delivery_id=?",(delivery['delivery_id'],));c.commit()
            client.app.state.report_delivery.initialize()
            delivery=checked(client,'GET',route(a)+'/deliveries',headers=h)['items'][0]
            assert delivery['status']=='unknown'
        a['_test_delivery']=delivery
    if explicit_parent_revoke:
        checked(client,'POST',route(a)+'/revoke',{'reason':'公开合成：家长明确撤销A'},h)
    checked(client,'POST',route(weekly)+'/feedback',{'helpfulness':4,'stress_rating':2,
        'notes':'公开合成：仅主观体验B','client_action_id':'cycle-feedback'},h)
    # The UI list reconciliation keeps the original period cutoff. A separate
    # explicit generate would intentionally pick a new supply as_of timestamp.
    rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
    b=next(x for x in rows if x['report_type']=='monthly' and x['status']=='draft')
    assert b['snapshot_id']!=a['snapshot_id'] and b['source_id']!=a['source_id']
    b=checked(client,'PUT',route(b),EDIT_B,h)
    old_a=checked(client,'GET',route(a),headers=h)
    assert old_a['status']=='revoked' and old_a['parent_edit']==a['parent_edit']
    checked(client,'POST',f'/v1/parent/children/{CHILD}/report-snapshots/{weekly["snapshot_id"]}/feedback-retractions',
        {'expected_revision':1,'client_action_id':'cycle-retract-feedback','reason':'公开合成：撤回主观体验B'},h)
    # Original get/list paths, not SQL or mocked fingerprints, restore source A.
    if finish_list:
        rows=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
    return h,a,b,old_a,rows

def test_http_source_A_B_A_requires_fresh_unapproved_draft(tmp_path):
    cfg=settings(tmp_path)
    with TestClient(create_app(cfg)) as client:
        h,a,b,old_a,rows=arrange_cycle(client)
        current=[x for x in rows if x['report_type']=='monthly' and x['status']=='draft'
                 and x['correction_status']['source_current'] is True]
        assert len(current)==1,'SOURCE_REVERSION_HAS_NO_FRESH_CURRENT_DRAFT'
        fresh=current[0]
        assert fresh['snapshot_id'] not in (a['snapshot_id'],b['snapshot_id'])
        assert fresh['source_id'] not in (a['source_id'],b['source_id'])
        assert fresh['facts']==a['facts'] and fresh['inferences']==a['inferences']
        assert fresh['content']==a['content'] and fresh['evidence_version']==a['evidence_version']
        assert fresh['parent_edit']!=a['parent_edit'] and fresh['approved_at'] is None
        assert fresh['parent_action']['binding_status']=='unknown'
        again=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
        assert [x['snapshot_id'] for x in again if x['report_type']=='monthly' and x['status']=='draft']==[fresh['snapshot_id']]
        retained=checked(client,'GET',route(a),headers=h)
        for key in ('facts','inferences','content','parent_edit','approved_at','approved_by','source_id','source_version'):
            assert retained[key]==old_a[key]
        assert retained['status']=='revoked'
        checked(client,'POST',route(a)+'/approve',{},h,status=409)
        # Saved B edits transfer only by a separate, version-bound parent command.
        old_b=checked(client,'GET',route(b),headers=h)
        assert old_b['status']=='revoked' and old_b['parent_edit']==b['parent_edit']
        moved=checked(client,'POST',route(fresh)+'/carry-saved-edits',
            {'source_snapshot_id':b['snapshot_id'],'source_edit_version':old_b['parent_edit_version'],
             'target_edit_version':fresh['parent_edit_version'],'client_action_id':'cycle-explicit-carry'},h)
        assert moved['parent_edit']==b['parent_edit'] and moved['status']=='draft'
        approved=checked(client,'POST',route(fresh)+'/approve',{},h)
        assert approved['status']=='approved' and approved['parent_action']['binding_status']=='bound'
        assert approved['source_version'].rsplit(':',1)[-1]==approved['source_id'].rpartition('@')[2]
        ids=(a['snapshot_id'],b['snapshot_id'],fresh['snapshot_id'])
    with TestClient(create_app(cfg)) as reopened:
        h={'X-HSC-Parent':checked(reopened,'POST','/v1/parent/unlock',{'pin':'2468'})['parent_token']}
        old_a=checked(reopened,'GET','/v1/parent/report-snapshots/'+ids[0],headers=h)
        old_b=checked(reopened,'GET','/v1/parent/report-snapshots/'+ids[1],headers=h)
        live=checked(reopened,'GET','/v1/parent/report-snapshots/'+ids[2],headers=h)
        assert old_a['status']==old_b['status']=='revoked'
        assert old_a['parent_edit']==a['parent_edit'] and old_b['parent_edit']==b['parent_edit']
        assert live['status']=='approved' and live['parent_edit']==b['parent_edit']
        assert live['correction_status']['source_current'] is True

@pytest.mark.parametrize('delivery_state',['scheduled','unknown'])
def test_reissue_preserves_original_delivery_obligations_without_auto_send(tmp_path,delivery_state):
    with TestClient(create_app(settings(tmp_path))) as client:
        h,a,b,_,rows=arrange_cycle(client,delivery_state=delivery_state)
        fresh=next(x for x in rows if x['report_type']=='monthly' and x['status']=='draft'
                   and x['correction_status']['source_current'] is True)
        history=checked(client,'GET',route(a)+'/deliveries',headers=h)['items']
        assert len(history)==1 and history[0]['delivery_id']==a['_test_delivery']['delivery_id']
        assert history[0]['status']==('cancelled' if delivery_state=='scheduled' else 'unknown')
        assert checked(client,'GET',route(fresh)+'/deliveries',headers=h)['items']==[]
        checked(client,'POST',route(a)+'/deliveries',{'recipient_id':history[0]['recipient_id']},h,status=409)
        checked(client,'POST',route(fresh)+'/deliveries',{'recipient_id':history[0]['recipient_id']},h,status=409)
        if delivery_state=='unknown':
            assert history[0]['recipient_id'] in fresh['correction_status']['recipient_ids_requiring_correction']
            assert fresh['correction_status']['automatic_delivery'] is False

def test_parent_explicit_revocation_does_not_trigger_automatic_reissue(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        h,a,b,old_a,rows=arrange_cycle(client,explicit_parent_revoke=True)
        assert not [x for x in rows if x['report_type']=='monthly' and x['status']=='draft'
                    and x['correction_status']['source_current'] is True]
        retained=checked(client,'GET',route(a),headers=h)
        assert retained['status']=='revoked' and retained['parent_edit']==old_a['parent_edit']
        checked(client,'POST',route(a)+'/approve',{},h,status=409)

def test_two_http_refreshes_share_one_issuance_and_explicit_revoke_stays_revoked(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        h,a,b,_,rows=arrange_cycle(client,finish_list=False)
        # The real original worker's persisted period read restores raw A without
        # creating a parent snapshot. Both HTTP requests then attempt first issue.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(client.app.state.period_reports.get,
                        b['source_id'].rpartition('@')[0],persist_refresh=True).result()
        def refresh(_):
            return checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(refresh,range(2)))
        first=next(x for x in results[0] if x['report_type']=='monthly' and x['status']=='draft'
                   and x['correction_status']['source_current'] is True)
        for result in results:
            assert [x['snapshot_id'] for x in result if x['report_type']=='monthly' and x['status']=='draft'
                    and x['correction_status']['source_current'] is True]==[first['snapshot_id']]
        other=checked(client,'GET','/v1/parent/children/child-primary-4/report-snapshots',headers=h)['items']
        assert not any(x['snapshot_id']==first['snapshot_id'] for x in other)
        checked(client,'POST',route(first)+'/revoke',{'reason':'公开合成：明确撤销新签发稿'},h)
        after=refresh(None)
        assert not [x for x in after if x['report_type']=='monthly' and x['status']=='draft'
                    and x['correction_status']['source_current'] is True]

def test_non_system_supersession_cannot_authorize_reissue(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        h,a,b,_,rows=arrange_cycle(client,finish_list=False)
        # Corrupt only a new public synthetic witness, not production/private data.
        with client.app.state.parent_reports.database.connect() as c:
            c.execute("UPDATE audit_event SET actor='parent' WHERE action='parent_period_snapshot_superseded' AND child_id=? AND json_extract(summary_json,'$.snapshot_id')=?",(CHILD,a['snapshot_id']));c.commit()
        after=checked(client,'GET',f'/v1/parent/children/{CHILD}/report-snapshots',headers=h)['items']
        assert not [x for x in after if x['report_type']=='monthly' and x['status']=='draft'
                    and x['correction_status']['source_current'] is True]

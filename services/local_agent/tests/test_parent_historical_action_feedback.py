"""Public real HTTP/Database regression: feedback order is not an authorization."""
import json
import hashlib
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.report_followup import ReportFollowupService

CHILD = 'child-junior-3'
OTHER = 'child-primary-4'


def checked(client, method, path, body=None, headers=None, status=200):
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status, (response.status_code, response.text[:400])
    return response.json()


def seed(client):
    sid = checked(client, 'POST', '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'historical-public-session'}, status=201)['session_id']
    def move(before, after):
        checked(client, 'POST', f'/v1/sessions/{sid}/transition', {'expected_state': before, 'target_state': after,
            'reason': 'synthetic_native_fixture', 'client_action_id': 'historical-public:'+after})
    move('check_in', 'classroom_recall')
    checked(client, 'POST', f'/v1/sessions/{sid}/recalls/none', {'client_action_id': 'historical-public-no-recall'})
    move('classroom_recall', 'homework_capture')
    checked(client, 'POST', f'/v1/sessions/{sid}/homework/none', {'client_action_id': 'historical-public-no-homework'})
    checked(client, 'POST', f'/v1/sessions/{sid}/plan/generate', {'client_action_id': 'historical-public-plan'})
    move('plan_review', 'study'); move('study', 'reflection')
    result = checked(client, 'POST', f'/v1/sessions/{sid}/reflection/complete', {'learned_text': '公开合成：未开始学习任务。',
        'difficult_text': '暂时想不起课堂内容。', 'feeling': '平静', 'next_action': '下次先核对任务。',
        'skipped': False, 'client_action_id': 'historical-public-reflection'})
    assert result['daily_report_ready'] and not any(x['status']=='completed' for x in result['plan_items'])


def current_period(client, headers, kind):
    checked(client, 'POST', f'/v1/children/{CHILD}/period-reports/generate?report_type={kind}', headers=headers)
    offered = checked(client, 'GET', f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers)['items']
    draft = next(x for x in offered if x['report_type']==kind and x['status']=='draft')
    review = '/v1/parent/report-snapshots/'+draft['snapshot_id']
    approved = checked(client, 'POST', review+'/approve', {'coaching_decision':'custom',
        'coaching_action':'公开合成核对一步', 'coaching_script':'公开合成先听你说', 'coaching_duration_minutes':2}, headers)
    return review, approved['parent_action']


def payload(action, key, revision=0):
    return {'action_snapshot_id':action['snapshot_id'], 'client_action_id':key, 'expected_revision':revision,
        'executed':True, 'executed_at':datetime.now(timezone.utc).isoformat(), 'effectiveness':3,
        'stress_rating':None, 'notes_summary':'公开合成家长报告，不证明实际效果'}


def test_experience_then_historical_actions_reopen_revise_retract_without_current_authority(tmp_path):
    path=tmp_path/'public.sqlite3'
    settings=Settings(data_dir=tmp_path, database_path=path, maintenance_enabled=False,
        model_api_key=None, fallback_model_api_key=None, cloud_asr_provider='disabled', tts_provider='disabled')
    rows=[]
    with TestClient(create_app(settings)) as client:
        seed(client)
        h={'X-HSC-Parent':checked(client,'POST','/v1/parent/setup',{'pin':'2468'})['parent_token']}
        for kind in ('weekly','monthly'):
            review,action=current_period(client,h,kind)
            root=f"/v1/parent/children/{CHILD}/parent-actions/{action['snapshot_id']}"
            checked(client,'POST',review+'/feedback',{'helpfulness':4,'stress_rating':2,'notes':'公开合成报告体验',
                'client_action_id':'experience:'+kind},h)
            # Normal order: report experience first, later the originally approved action.
            request=payload(action,'action:'+kind)
            receipt=checked(client,'PUT',root+'/feedback',request,h)
            assert receipt['evidence_kind']=='parent_reported_action' and receipt['action_snapshot_id']==action['snapshot_id']
            assert receipt['client_action_id']!='experience:'+kind
            assert checked(client,'PUT',root+'/feedback',request,h)==receipt
            checked(client,'GET',root.replace(CHILD,OTHER),headers=h,status=404)
            checked(client,'PUT',root.replace(CHILD,OTHER)+'/feedback',request,h,status=404)
            checked(client,'PUT',root+'/feedback',{**request,'action_snapshot_id':'wrong'},h,status=409)
            checked(client,'PUT',root+'/feedback',{**request,'client_action_id':'stale:'+kind},h,status=409)
            # Original regeneration creates an authenticated system supersession, not a reason-string grant.
            checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type={kind}',headers=h)
            historical=checked(client,'GET',review,headers=h)
            assert historical['status']=='revoked' and historical['parent_action']['binding_status']=='unknown'
            history_action=next(a for a in historical['historical_parent_actions'] if a['snapshot_id']==action['snapshot_id'])
            assert history_action['feedback_eligibility']=={'record_allowed':True,'approval_witness_valid':True,
                'current_execution_granted':False,'reason':'system_superseded_approval'}
            # Historical self-report never becomes current model/plan evidence or authorizes new caps.
            database=Database(path); service=ParentActionEvidenceService(database)
            with database.connect() as c:
                frozen=json.loads(c.execute('SELECT payload_json FROM parent_action_snapshot WHERE snapshot_id=?',(action['snapshot_id'],)).fetchone()[0])
                assert service._review_current(c,frozen) is False
                assert not any(x['action_snapshot_id']==action['snapshot_id'] for x in service.feedback_for_child(c,CHILD))
            tomorrow=(family_today()+timedelta(days=1)).isoformat()
            blocked=client.post(f"/v1/parent/children/{CHILD}/report-snapshots/{action['review_binding']['snapshot_id']}/plan-adjustments",
                headers=h,json={'expected_feedback_revision':1,'constraint_type':'daily_total_minutes_cap','value_minutes':20,
                'start_date':tomorrow,'end_date':tomorrow,'client_action_id':'stale-cap:'+kind})
            assert blocked.status_code==409
            rows.append((review,root,action,request,receipt))
        # Feedback on the second approved action does not prevent revising the first.
        first=rows[0]
        revised=checked(client,'PUT',first[1]+'/feedback',{**first[3],'client_action_id':'revise-first-after-second',
            'expected_revision':1,'effectiveness':4},h)
        assert revised['revision']==2
    # New real app + Database reopen; never a fake persistence adapter.
    with TestClient(create_app(settings)) as client:
        h={'X-HSC-Parent':checked(client,'POST','/v1/parent/unlock',{'pin':'2468'})['parent_token']}
        for index,(review,root,action,request,receipt) in enumerate(rows):
            history=checked(client,'GET',root+'/history',headers=h)['items']
            assert history[0]==receipt and len(history)==(2 if index==0 else 1)
            checked(client,'POST',review+'/revoke',{'reason':'后续证据已更新；请审核新版草稿'},h)
            # Even spoofing the system reason cannot bypass explicit parent withdrawal.
            current=checked(client,'GET',root,headers=h)
            assert current['feedback_eligibility']['record_allowed'] is False
            checked(client,'PUT',root+'/feedback',{**request,'client_action_id':'denied-after-parent-revoke:'+str(index),
                'expected_revision':len(history)},h,status=409)
            retract=checked(client,'PUT',root+'/feedback',{'action_snapshot_id':action['snapshot_id'],
                'expected_revision':len(history),'client_action_id':'retract:'+str(index),'operation':'retract','reason':'公开合成撤回'},h)
            assert retract['operation']=='retract'
            assert checked(client,'GET',root+'/history',headers=h)['items'][-1]==retract


def test_historical_witness_rejects_corrupt_binding_and_untrusted_supersession(tmp_path):
    path=tmp_path/'public.sqlite3'
    settings=Settings(data_dir=tmp_path,database_path=path,maintenance_enabled=False,
        model_api_key=None,fallback_model_api_key=None,cloud_asr_provider='disabled',tts_provider='disabled')
    with TestClient(create_app(settings)) as client:
        seed(client);h={'X-HSC-Parent':checked(client,'POST','/v1/parent/setup',{'pin':'2468'})['parent_token']}
        review,action=current_period(client,h,'weekly')
        checked(client,'POST',review+'/feedback',{'helpfulness':4,'client_action_id':'public-experience'},h)
        checked(client,'POST',f'/v1/children/{CHILD}/period-reports/generate?report_type=weekly',headers=h)
        service=ParentActionEvidenceService(Database(path))
        with service.database.connect() as c:
            row=c.execute('SELECT * FROM parent_action_snapshot WHERE snapshot_id=?',(action['snapshot_id'],)).fetchone()
            frozen=json.loads(row['payload_json'])
            assert service._historical_feedback_eligibility(c,row,frozen)['record_allowed'] is True
            def altered_row(value):
                encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False)
                return {**dict(row),'fingerprint':hashlib.sha256(encoded.encode()).hexdigest()}
            for field,value in [('child_id',OTHER),('report_id','foreign'),('source_version','forged')]:
                bad={**frozen,field:value}
                assert service._historical_feedback_eligibility(c,altered_row(bad),bad)['record_allowed'] is False
            for field in ('snapshot_id','source_version','approval_event_id','edit_fingerprint','base_action_fingerprint','decision'):
                bad={**frozen,'review_binding':{**frozen['review_binding'],field:'forged'}}
                assert service._historical_feedback_eligibility(c,altered_row(bad),bad)['record_allowed'] is False
            assert service._historical_feedback_eligibility(c,{**dict(row),'fingerprint':'0'*64},frozen)['record_allowed'] is False
            # Fault injection in this generated synthetic DB only, rolled back each time.
            mutations=[("UPDATE audit_event SET actor='parent' WHERE action='parent_period_snapshot_superseded'",()),
                ("UPDATE audit_event SET occurred_at='2000-01-01T00:00:00+00:00' WHERE action='parent_period_snapshot_superseded'",()),
                ("UPDATE audit_event SET summary_json=? WHERE action='parent_period_snapshot_superseded'",(json.dumps({'snapshot_id':action['review_binding']['snapshot_id'],'replacement_snapshot_id':'wrong'}),)),
                ("UPDATE parent_report_snapshot SET approved_by='other' WHERE snapshot_id=?",(action['review_binding']['snapshot_id'],)),
                ("UPDATE parent_report_snapshot SET approved_at='2000-01-01T00:00:00+00:00' WHERE snapshot_id=?",(action['review_binding']['snapshot_id'],)),
                ("UPDATE parent_report_snapshot SET facts_json='{}' WHERE snapshot_id=?",(action['review_binding']['snapshot_id'],))]
            for sql,args in mutations:
                c.execute('SAVEPOINT public_fault');c.execute(sql,args)
                assert service._historical_feedback_eligibility(c,row,frozen)['record_allowed'] is False, sql
                c.execute('ROLLBACK TO public_fault');c.execute('RELEASE public_fault')
            assert service._historical_feedback_eligibility(c,row,frozen)['record_allowed'] is True


# The original learning/report services and existing real HTTP fixture own all
# facts below; no snapshot/receipt is invented to exercise this boundary.
import pytest
from test_period_report_reliable_evidence import (flow, learning_clock, answer,
    CHILD as PERIOD_CHILD, OTHER as PERIOD_OTHER, SECOND)
from test_period_adjustment_rebinding_http import journey


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_original_learning_withdrawal_blocks_new_historical_feedback_but_keeps_retract(journey, kind, monkeypatch):
    f, settings, client, headers = journey
    # Learning time remains in the same original report period. Approval and
    # supersession audit time follows the real action-freeze clock, as in production.
    from home_study_agent import parent_reports
    monkeypatch.setattr(parent_reports, "utc_now", lambda: datetime.now(timezone.utc).isoformat())
    child = PERIOD_CHILD
    answer(f, '90')
    checked(client, 'POST', f'/v1/children/{child}/period-reports/generate?report_type={kind}', headers=headers)
    offered = checked(client, 'GET', f'/v1/parent/children/{child}/report-snapshots', headers=headers)['items']
    draft = next(x for x in offered if x['report_type'] == kind and x['status'] == 'draft')
    assert draft['facts']['learning_summary']['coverage']['eligible_evidence_count'] == 1
    original_ref = draft['facts']['learning_summary']['evidence'][0]['evidence_ref']
    review = '/v1/parent/report-snapshots/' + draft['snapshot_id']
    approved = checked(client, 'POST', review + '/approve', {'coaching_decision': 'custom',
        'coaching_action': '公开合成核对原来的一步', 'coaching_duration_minutes': 2}, headers)
    action = approved['parent_action']
    root = f"/v1/parent/children/{child}/parent-actions/{action['snapshot_id']}"
    frozen = (action['coaching'], action['review_binding'], action['version'])
    # A normal report-experience rating is not withdrawal of a factual source.
    checked(client, 'POST', review + '/feedback', {'helpfulness': 4, 'stress_rating': 2,
        'notes': '公开合成报告体验', 'client_action_id': 'source-experience:' + kind}, headers)
    first = checked(client, 'PUT', root + '/feedback', payload(action, 'source-action:' + kind), headers)
    assert first['revision'] == 1
    # Later independent learning adds facts. The original references remain
    # active, so historical self-report must not require whole-current-FP equality.
    answer(f, '42', item=SECOND)
    checked(client, 'POST', f'/v1/children/{child}/period-reports/generate?report_type={kind}', headers=headers)
    old = checked(client, 'GET', review, headers=headers)
    assert old['status'] == 'revoked' and old['correction_status']['source_current'] is False
    second = checked(client, 'PUT', root + '/feedback', payload(action, 'after-new-learning:' + kind, 1), headers)
    assert second['revision'] == 2
    checked(client, 'POST', f'/v1/parent/children/{PERIOD_OTHER}/period-evidence/{original_ref}/retract',
        {'note': '不属于另一孩子', 'client_action_id': 'foreign-source:' + kind}, headers, status=404)
    checked(client, 'POST', f'/v1/parent/children/{child}/period-evidence/{original_ref}/retract',
        {'note': '公开合成明确撤回原事实', 'client_action_id': 'withdraw-original-fact:' + kind}, headers)
    denied = client.put(root + '/feedback', headers=headers,
        json=payload(action, 'after-source-withdrawal:' + kind, 2))
    assert denied.status_code == 409 and 'PARENT_ACTION_REVIEW_NOT_CURRENT' in denied.text
    original = checked(client, 'GET', root, headers=headers)
    assert original['feedback_eligibility'] == {'record_allowed': False, 'approval_witness_valid': True,
        'current_execution_granted': False, 'reason': 'invalid_approval_witness'}
    assert (original['coaching'], original['review_binding'], original['version']) == frozen
    service = ParentActionEvidenceService(Database(f.db.path))
    with f.db.connect() as c:
        assert not any(x['action_snapshot_id'] == action['snapshot_id'] for x in service.feedback_for_child(c, child))
    # Close the original app/DB lifecycle and reopen the actual persisted state.
    client.finish_original_lifecycle()
    with TestClient(create_app(settings)) as reopened:
        h = {'X-HSC-Parent': checked(reopened, 'POST', '/v1/parent/unlock', {'pin': '2468'})['parent_token']}
        history = checked(reopened, 'GET', root + '/history', headers=h)['items']
        assert history == [first, second]
        denied = reopened.put(root + '/feedback', headers=h, json=payload(action, 'restart-denied:' + kind, 2))
        assert denied.status_code == 409 and 'PARENT_ACTION_REVIEW_NOT_CURRENT' in denied.text
        withdrawal = checked(reopened, 'PUT', root + '/feedback', {'action_snapshot_id': action['snapshot_id'],
            'client_action_id': 'withdraw-feedback-after-source:' + kind, 'expected_revision': 2,
            'operation': 'retract', 'reason': '公开合成已有自述仍可撤回'}, h)
        assert withdrawal['operation'] == 'retract' and withdrawal['revision'] == 3
        assert checked(reopened, 'GET', root + '/history', headers=h)['items'] == [first, second, withdrawal]


def test_frozen_learning_ref_schema_cannot_treat_malformed_as_empty(flow):
    from copy import deepcopy
    from test_period_parent_action_evidence import approved
    f = flow
    _, saved = approved(f)
    action = saved['parent_action']
    service = ParentActionEvidenceService(f.db)
    canon = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with f.db.connect() as c:
        row = dict(c.execute('SELECT * FROM parent_action_snapshot WHERE snapshot_id=?', (action['snapshot_id'],)).fetchone())
        frozen = json.loads(row['payload_json'])
        original = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (saved['snapshot_id'],)).fetchone())
        assert service._historical_feedback_eligibility(c, row, frozen)['record_allowed'] is True
        for replacement in (None, {}, '', 0):
            facts = json.loads(original['facts_json'])
            facts['learning_summary']['knowledge_support'] = replacement
            inferences = json.loads(original['inferences_json']); content = json.loads(original['content_json'])
            source = frozen['source_version']
            version_payload = {'facts': facts, 'inferences': inferences, 'content': content['summary'],
                'period_end': content['report_date'], 'source': source, 'template': original['template_version']}
            version = hashlib.sha256(canon(version_payload).encode()).hexdigest()[:16]
            changed_version = source + ':' + version
            evidence_version = 'sha256:' + hashlib.sha256(canon({'facts': facts, 'inferences': inferences}).encode()).hexdigest()
            c.execute('SAVEPOINT malformed_frozen_learning')
            try:
                c.execute('UPDATE parent_report_snapshot SET facts_json=?,evidence_version=?,source_version=?,source_id=? WHERE snapshot_id=?',
                    (canon(facts), evidence_version, changed_version, row['period_report_id'] + '@' + version, saved['snapshot_id']))
                changed = deepcopy(frozen); changed['review_binding']['source_version'] = changed_version
                owned_row = {**row, 'fingerprint': hashlib.sha256(canon(changed).encode()).hexdigest()}
                assert service._historical_feedback_eligibility(c, owned_row, changed)['record_allowed'] is False
            finally:
                c.execute('ROLLBACK TO malformed_frozen_learning'); c.execute('RELEASE malformed_frozen_learning')

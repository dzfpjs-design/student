import json

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.parent_supply_followup import current_followups, report_supply_facts
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_supply_gap_resume_http import supplied, CHILD
from test_cross_day_parent_summary_audit import daytime
from test_supply_obligation_cross_day import close_and_plan, resume_next_day


def prepared(case, monkeypatch):
    sid, root, workspace, attempts = close_and_plan(case, monkeypatch)
    repo = StudyRepository(case['db'])
    summary, coaching = build_daily_report(repo.report_evidence(case['sid']))
    report = repo.save_daily_report(case['sid'], summary, coaching)
    service = ParentReportService(case['db'])
    snapshot = service.create_from_daily(report)
    return sid, root, report, snapshot, service, attempts


def read(case, root):
    path=f"/v1/parent/children/{CHILD}/supply-followups/{root['decision_id']}"
    response=case['client'].get(path)
    assert response.status_code==200,response.text
    assert response.headers['Cache-Control']=='no-store'
    return path,response.json()


def test_report_frozen_local_current_budget_auth_and_reopen(supplied, monkeypatch):
    case=supplied
    sid, root, report, snapshot, service, attempts=prepared(case,monkeypatch)
    service.approve(snapshot['snapshot_id'])
    path,ready=read(case,root)
    assert ready['status']=='ready'
    assert ready['original_session_id']==case['sid'] and ready['current_session_id']==sid
    assert root['reason']==ready['original_reason']
    with case['db'].connect() as c:
        c.execute('BEGIN'); before=c.total_changes
        rows=current_followups(c,CHILD)
        assert c.total_changes==before
        writing=[r for r in rows if '写作' in r['title']]
        assert len(writing)>=4 and len({r['root_obligation_id'] for r in writing})==len(writing)
        frozen=dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone())
    response=case['client'].get('/v1/parent/work-queue',params={'child_id':CHILD,'limit':2})
    assert response.status_code==200,response.text
    page=response.json();assert page['next_cursor'] and page['total']>2
    assert page['items'][0]['kind']=='supply_followup'
    assert all(r['bulk_action'] is None for r in page['items'])
    with case['db'].connect() as c:
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='child-junior-3' AND status='active'").fetchone()
    other=case['client'].get(path.replace(CHILD,'child-junior-3'))
    assert other.status_code==404
    token=case['client'].headers.pop('X-HSC-Parent')
    assert case['client'].get(path).status_code==401
    case['client'].headers['X-HSC-Parent']=token
    assert case['client'].post(path,json={}).status_code==405
    rule=case['client'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
    values={k:rule[k] for k in ('latest_end_time','max_learning_minutes','task_segment_minutes','break_minutes','pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')}
    changed=case['client'].put(f'/v1/parent/children/{CHILD}/family-rule',json={**values,'task_segment_minutes':10})
    assert changed.status_code==200,changed.text
    _,blocked=read(case,root);assert blocked['status']=='waiting_constraints' and blocked['version']!=ready['version']
    assert case['client'].get('/v1/parent/work-queue',params={'child_id':CHILD,'limit':2,'cursor':page['next_cursor']}).status_code==409
    with case['db'].connect() as c:
        assert dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone())==frozen
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==attempts
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0]==0
    case['client'].__exit__(None,None,None)
    with TestClient(create_app(case['settings'],model_gateway=case['gateway']),client=('127.0.0.1',41604),
        headers={'X-HSC-Session':case['settings'].session_token}) as client:
        assert client.get(path).status_code==401
        token=client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        client.headers['X-HSC-Parent']=token
        reopened=client.get(path)
        assert reopened.status_code==200,reopened.text
        assert reopened.json()['status']=='waiting_constraints'
        with case['db'].connect() as c:
            assert dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone())==frozen


def test_withdrawal_does_not_rewrite_asof_and_removed_root_not_readable(supplied,monkeypatch):
    case=supplied
    sid,root,report,snapshot,service,attempts=prepared(case,monkeypatch)
    path,ready=read(case,root);assert ready['status']=='ready'
    before=report['summary']['supply_obligations_as_of']
    response=case['client'].post(case['base']+f"/{case['published']['candidate_id']}/withdraw",json={
        'expected_version':4,'action_id':'parent-followup-withdraw','note':'合成来源撤回'})
    assert response.status_code==200,response.text
    _,current=read(case,root)
    assert current['status'] in {'missing_source','source_changed'} and current['version']!=ready['version']
    with case['db'].connect() as c:
        assert report_supply_facts(c,CHILD,case['sid'])==before
        frozen=json.loads(c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0])
        assert frozen['summary']['supply_obligations_as_of']==before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==attempts
    # Missing/deleted roots are not substituted by the next same-skill item.
    assert case['client'].get(path.replace(root['decision_id'],'deleted-root')).status_code==404


def test_current_read_does_not_replay_started_task(supplied,monkeypatch):
    case=supplied
    sid,root,report,snapshot,service,attempts=prepared(case,monkeypatch)
    case['post'](case['client'],f'/v1/sessions/{sid}/transition',dict(expected_state='plan_review',target_state='study',
        reason='确认今日计划',client_action_id='parent-followup-study'))
    turn,response=resume_next_day(case,monkeypatch,sid)
    assert response.status_code==200,response.text
    path,current=read(case,root);assert current['status']=='active'
    for _ in range(2):
        assert case['client'].get(path).json()['plan_item_id']==current['plan_item_id']
    with case['db'].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(current['plan_item_id'],)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==attempts


def test_actual_report_erasure_preserves_original_then_learning_erasure_removes_it(supplied, monkeypatch):
    case = supplied
    sid, root, report, snapshot, service, attempts = prepared(case, monkeypatch)
    path, original = read(case, root)
    with case['db'].connect() as c:
        other = dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-junior-3'").fetchone())
    for scope in ('reports', 'all_history'):
        body = dict(scope=scope, confirmation='确认删除', client_action_id='followup-delete-'+scope)
        result = case['client'].post(f'/v1/parent/children/{CHILD}/delete', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'completed', result.json()
        repeated = case['client'].post(f'/v1/parent/children/{CHILD}/delete', json=body)
        assert repeated.status_code == 200 and repeated.json()['status'] == 'completed'
        response = case['client'].get(path)
        if scope == 'reports':
            assert response.status_code == 200 and response.json()['root_obligation_id'] == original['root_obligation_id']
        else:
            assert response.status_code == 404
        with case['db'].connect() as c:
            assert dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-junior-3'").fetchone()) == other
            assert not c.execute('PRAGMA foreign_key_check').fetchall()

"""Real report/action persistence around a currently qualified wrong answer."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_daily_report_evidence_versions import CHILD, ITEM
from family_budget_clock import next_family_evening


@pytest.fixture
def weak_report(tmp_path):
    db = Database(tmp_path/'weak-action.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    GoalProgramService(db).seed_defaults()
    sid = repo.start_session(CHILD,str(uuid4()))['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    answer = learning.submit_diagnostic(sid,ITEM,'91','qualified-wrong-answer')
    assert answer['score_status']=='verified_local'
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?",(sid,))
        c.commit()
    repo.complete_with_reflection(sid,'我完成了这次练习','','okay','',False,'finish-weak-action')
    evidence = repo.report_evidence(sid)
    wrong = next(item for item in evidence['knowledge_evidence'] if item['correctness']<1)
    summary,coaching = build_daily_report(evidence)
    saved = repo.save_daily_report(sid,summary,coaching)
    return db,repo,sid,saved,wrong


def test_qualified_wrong_answer_reaches_specific_frozen_parent_action_and_restart(weak_report):
    db,repo,_,saved,wrong = weak_report
    action = saved['parent_coaching']
    assert action['focus']=='verified_learning_check'
    assert wrong['knowledge_title'] in action['action']['what']
    assert action['action']['learning_target']['evidence_id']==wrong['evidence_id']
    assert action['action']['learning_target']['mastery_claim_allowed'] is False
    assert '没有记录具体困难' not in action['scenario']
    assert '数学' in saved['summary']['facts'][0]
    assert saved['parent_action']['coaching']==action
    reopened = StudyRepository(Database(db.path)).get_daily_report(saved['report_id'])
    assert reopened['parent_action']['snapshot_id']==saved['parent_action']['snapshot_id']
    assert reopened['parent_action']['coaching']==action


def test_retired_source_removes_current_action_target_without_erasing_frozen_history(weak_report):
    db,repo,_,saved,wrong = weak_report
    assert saved['parent_coaching']['focus']=='verified_learning_check'
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(ITEM,))
        c.commit()
    current = repo.get_daily_report(saved['report_id'])
    assert current['parent_coaching']['focus']!='verified_learning_check'
    frozen = ParentActionEvidenceService(Database(db.path)).get(CHILD,saved['parent_action']['snapshot_id'])
    assert frozen['coaching']['action']['learning_target']['evidence_id']==wrong['evidence_id']


@pytest.mark.parametrize('protection',['skip','no_homework','child_next_action','unqualified'])
def test_weakness_never_overrides_choice_or_unqualified_evidence(weak_report,protection):
    _,repo,sid,_,_ = weak_report
    evidence = repo.report_evidence(sid)
    if protection=='skip':
        evidence['reflection']['skipped']=True
    elif protection=='no_homework':
        evidence['session']['homework_declared_empty']=True
    elif protection=='child_next_action':
        evidence['reflection']['next_action']='明天先整理材料'
    else:
        evidence['reliable_learning']['items']=[]
    _,coaching = build_daily_report(evidence)
    assert coaching['focus']!='verified_learning_check'
    assert 'learning_target' not in coaching['action']


def record_action(repo,saved,**changes):
    return repo.save_parent_report_feedback(saved['report_id'],**{
        'executed':True,'executed_at':saved['parent_action']['created_at'],
        'effectiveness':2,'stress_rating':5,'notes_summary':'合成家长报告：只进行了短时沟通',
        'action_snapshot_id':saved['parent_action']['snapshot_id'],
        'expected_revision':0,'client_action_id':'weak-action-feedback',**changes})


def test_bound_action_feedback_changes_comparable_guidance_and_retraction_restores_it(weak_report):
    db,repo,sid,saved,_ = weak_report
    receipt = record_action(repo,saved)
    assert record_action(repo,saved)['feedback_id']==receipt['feedback_id']
    reopened = StudyRepository(Database(db.path))
    evidence = reopened.report_evidence(sid)
    _,adjusted = build_daily_report(evidence)
    assert adjusted['adaptation']['feedback_count']==1
    assert adjusted['action']['duration_minutes']<saved['parent_coaching']['action']['duration_minutes']
    assert adjusted['action']['learning_target']['mastery_claim_allowed'] is False
    mismatched = dict(evidence)
    mismatched['coaching_feedback']=[{**item,'feedback_context':{**item['feedback_context'],'target_knowledge_node_id':'other-node'}} for item in evidence['coaching_feedback']]
    assert build_daily_report(mismatched)[1]['adaptation']['feedback_count']==0
    revoked = record_action(reopened,saved,executed=None,executed_at=None,effectiveness=None,stress_rating=None,
        operation='retract',reason='合成撤回：选错记录',expected_revision=1,client_action_id='withdraw-weak-action')
    assert revoked['operation']=='retract'
    _,current = build_daily_report(reopened.report_evidence(sid))
    assert current['adaptation']['feedback_count']==0
    assert current['action']['duration_minutes']==saved['parent_coaching']['action']['duration_minutes']
    history = ParentActionEvidenceService(Database(db.path)).history(CHILD,saved['parent_action']['snapshot_id'])
    assert [row['operation'] for row in history]==['record','retract']


def test_actual_next_day_plan_records_action_and_obeys_only_explicit_parent_cap(weak_report,monkeypatch):
    from home_study_agent import repository as repository_module
    from home_study_agent.parent_reports import ParentReportService
    from home_study_agent.report_followup import ReportFollowupService
    from test_report_followup_host import intake
    db,repo,_,saved,_ = weak_report
    action_receipt = record_action(repo,saved)
    snapshots = ParentReportService(db)
    snapshots.initialize()
    frozen = snapshots.approve(snapshots.create_from_daily(repo.get_daily_report(saved['report_id']))['snapshot_id'])
    # Report experience and execution feedback are separate explicit records.
    snapshots.record_feedback(frozen['snapshot_id'],4,None,'合成家长明确另行限制明日负担','report-rating')
    followup = ReportFollowupService(db)
    followup.initialize()
    tomorrow, day = next_family_evening(monkeypatch)
    before = followup.project_for_plan(CHILD,day)
    assert before['constraints']==[]  # Pressure alone must never become an authorization.
    proposal = followup.propose_plan_adjustment(CHILD,frozen['snapshot_id'],1,'daily_total_minutes_cap',20,day,day,'weak-cap')
    followup.review_plan_adjustment(CHILD,proposal['adjustment_id'],1,'approve','approve-weak-cap')
    sid = intake((db,repo,saved,None))
    plan = repo.generate_plan(sid,'next-day-weakness-plan')
    assert plan['plan_items']
    with db.connect() as c:
        row = c.execute('SELECT receipt_id FROM report_plan_feedback_receipt WHERE child_id=?',(CHILD,)).fetchone()
    stored = ReportFollowupService(Database(db.path)).get_plan_receipt(CHILD,row[0])
    assert stored['constraints_satisfied'] and stored['inputs_current']
    assert any(item.get('feedback_id')==action_receipt['feedback_id'] for item in stored['inputs']['items'])
    assert not stored['learning_effect_verified']
    assert sum(item['expected_minutes'] for item in plan['plan_items'] if item['status'] not in ('cancelled','rescheduled'))<=20
    record_action(repo,saved,executed=None,executed_at=None,effectiveness=None,stress_rating=None,
        operation='retract',reason='撤回实际行动反馈',expected_revision=1,client_action_id='retract-after-plan')
    changed = ReportFollowupService(Database(db.path)).get_plan_receipt(CHILD,row[0])
    assert changed['inputs_current'] is False
    assert changed['plan_current'] is True
    assert len(followup.project_for_plan(CHILD,day)['constraints'])==1  # Separate approved safety limit remains.
    followup.review_plan_adjustment(CHILD,proposal['adjustment_id'],2,'retract','release-cap')
    assert ReportFollowupService(Database(db.path)).project_for_plan(CHILD,day)['constraints']==[]


def test_real_parent_http_reads_frozen_weakness_action_and_restarts_retraction(weak_report,tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db,repo,_,saved,wrong = weak_report
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False)
    path = f"/v1/parent/reports/{saved['report_id']}"
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        # Startup may add source catalogs; bind only the real current report.
        current = repo.get_daily_report(saved['report_id'])
        action = current['parent_action']
        assert action['coaching']['focus']=='verified_learning_check'
        assert client.get(path+'/actions').status_code==401
        token = client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        headers = {'X-HSC-Parent':token}
        rows = client.get(path+'/actions',headers=headers).json()['items']
        frozen = next(item for item in rows if item['snapshot_id']==action['snapshot_id'])
        assert frozen['coaching']['action']['learning_target']['evidence_id']==wrong['evidence_id']
        body = {'action_snapshot_id':action['snapshot_id'],'client_action_id':'http-weakness-feedback',
            'expected_revision':0,'executed':True,'executed_at':action['created_at'],
            'effectiveness':2,'stress_rating':5,'notes_summary':'合成家长反馈，不是孩子掌握证明'}
        response = client.put(path+'/feedback',headers=headers,json=body)
        assert response.status_code==200,response.text
        assert client.put(path+'/feedback',headers=headers,json=body).json()==response.json()
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        token = client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
        headers = {'X-HSC-Parent':token}
        rows = client.get(path+'/actions',headers=headers).json()['items']
        reopened = next(item for item in rows if item['snapshot_id']==action['snapshot_id'])
        assert reopened['feedback']['feedback_id']==response.json()['feedback_id']
        assert reopened['coaching']==frozen['coaching']
        retracted = client.put(path+'/feedback',headers=headers,json={
            'action_snapshot_id':action['snapshot_id'],'client_action_id':'http-weakness-retraction',
            'expected_revision':1,'operation':'retract','reason':'合成撤回：反馈绑定错误'})
        assert retracted.status_code==200,retracted.text
        assert retracted.json()['revision']==2
    service = ParentActionEvidenceService(Database(db.path))
    assert [item['operation'] for item in service.history(CHILD,action['snapshot_id'])]==['record','retract']

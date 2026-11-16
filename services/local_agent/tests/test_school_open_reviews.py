"""Synthetic reviewed originals; real published sources and parent review HTTP."""
import json
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.writing_rubric import prepare_writing_review
from test_open_response_supply_http import CHILD, prepared, publish_open
from test_pet_reviewed_source_actual_http import NoCloud
from test_pet_media_evidence import local_review
from test_writing_stage_review_consumers import advance_clock


def school_stage(supply, spec, stage):
    draft=supply.generate_draft(CHILD,{**spec,'stage':stage},author='local:school-source-author',
        action_id='school-draft-'+stage,expected_scope_version=2)
    checked=supply.validate(CHILD,draft['candidate_id'],actor='parent',action_id='school-check-'+stage,expected_version=1)
    context=checked['stage_review_context']
    proof={k:context[k] for k in ('schema','binding_version')}
    proof.update(stage=stage,source_quote=context['source_prompt'],candidate_quote=context['candidate_prompt'],
        same_skill_rationale='两题都要求识别条件、选择同一学科关系并给出可核对的推理与结论边界。',
        changed_context_rationale='近迁移改变具体条件与结论；远迁移从图像或实验现象反推条件，不复述原题。')
    supply.review(CHILD,draft['candidate_id'],actor='parent',decision='approve',note='Synthetic authored school source and changed context checked',
        stage_review=proof,action_id='school-review-'+stage,expected_version=2)
    return supply.publish(CHILD,draft['candidate_id'],actor='parent',action_id='school-publish-'+stage,expected_version=3)


@pytest.fixture
def school_case(tmp_path, monkeypatch, request):
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'open-supply.db'))
    monkeypatch.setattr('test_pet_media_evidence.CHILD', CHILD)
    monkeypatch.setattr('test_writing_stage_reviews.CHILD', CHILD)
    license_status = 'public' if getattr(request,'param',None) in {'public','spoken_public'} else 'family_owned_index'
    subject = '物理' if getattr(request,'param',None) in {'physics','spoken_physics'} else '数学'
    db, supply, spec, packet, _ = prepared(tmp_path, subject, license_status=license_status)
    if getattr(request,'param',None) in {'spoken','spoken_physics','spoken_public'}:
        packet = {**packet, 'response_mode':'spoken_explanation',
            'prompt':packet['prompt'].replace('写出','口头说明')}
    questions = (['两根导线材料和长度相同但横截面积不同，设计检验电阻差异的实验，并说明控制变量。',
        '实验发现导线变长时电阻增加，请从实验记录反推其他变量必须满足的条件，并说明结论的边界。'] if subject=='物理' else
        ['分析 x²-4x+4=0 在实数范围内的解，并写出判别依据和完整推理过程。',
        '一个二次函数的图像与横轴没有交点，说明系数间应满足的条件，用判别式支持你的论证。'])
    rows = [packet,*[{**packet,'prompt':question} for question in questions]]
    source = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
        title='Synthetic school transfer questions', subject=subject, grade_band='初三',
        version_label='isolated-open-2026', source_ref='isolated:school-review',
        license_status=license_status, parent_reviewed=True,
        content_text='\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)))
    with db.connect() as c:
        scope = json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection WHERE child_id=?', (CHILD,)).fetchone()[0])
        excerpts = [r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (source['source_id'],))]
    supply.select_curriculum(CHILD, {**scope, 'textbook_source_id': source['source_id']},
        actor='parent', action_id='review-scope', expected_version=1)
    baseline = publish_open(supply, {**spec, 'excerpt_id': excerpts[0]})
    near = school_stage(supply, {**spec, 'excerpt_id': excerpts[1]}, 'near_transfer')
    far = school_stage(supply, {**spec, 'excerpt_id': excerpts[2]}, 'far_transfer')
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'8' * 32)
    from home_study_agent.database import LocalMediaRuntime
    db.local_media_runtime = LocalMediaRuntime(settings.data_dir, settings.data_encryption_key)
    from test_cloud_first_agent import Planner
    gateway = Planner('task.start', {'start_selector': {
        'subject': None,
        'task_kind': 'review',
        'source_type': None,
        'target_quote': '开始今天的复习任务',
    }}, '开始今天的复习任务')
    with ExitStack() as stack:
        def reopen():
            stack.close()
            client = stack.enter_context(TestClient(create_app(settings, model_gateway=gateway), client=('127.0.0.1',41820)))
            client.school_gateway = gateway
            client.school_reopen = reopen
            return client
        client = reopen()
        token = client.post('/v1/parent/setup', json={'pin': '8264'}).json()['parent_token']
        sid = StudyRepository(db).start_session(CHILD, 'school-original')['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        yield db, client, {'X-HSC-Parent': token}, sid, baseline, near, far, supply, settings


def answer_and_review(case, *, item=None, prompt=None, score=0, key='baseline', submit=True):
    db, client, headers, sid, baseline = case[:5]
    if prompt is None:
        prompt = DiagnosticPromptService(db).prepare(sid, track='school',
            preferred_item_id=(item or baseline)['diagnostic_item_id'], client_action_id=key+'-prepare')['prompt']
    text = '我先检查二次项系数，再计算判别式，根据符号说明实数根的个数，并核对结论适用条件。' + key
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], text=text, client_action_id=key+'-original')
    scored = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer=text, writing_original_artifact_id=original['artifact_id'], client_action_id=key+'-answer')
    assert scored['attempt']['correctness'] is None
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (datetime.now(timezone.utc).isoformat(), CHILD))
        c.commit()
    assessment = local_review((db, None, sid, prompt, None, None), prompt, text, approve=False, scored=scored)
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (datetime.now(timezone.utc).isoformat(), CHILD))
        c.commit()
    with db.connect() as c:
        binding = prepare_writing_review(c, CHILD, assessment['assessment_id'])
    body = dict(expected_revision=0, client_action_id=key+'-review', operation='approve',
        reason='Synthetic original task and criteria checked; not an exam qualification.',
        independent=True, hint_level=0, review_profile='school_open_written_v1',
        writing_evidence={'binding_version': binding['binding_version'],
            'task_point_evidence': [{'task_point': p, 'response_quote': text} for p in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'], score=score, response_quotes=[text],
            rationale='Checked the original response against the stated criterion') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=p, response_quote=text) for p in assessment['criteria']])
    path = f'/v1/parent/children/{CHILD}/learning-rubrics/{assessment["assessment_id"]}/review'
    if submit:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
    return prompt, scored, assessment, path, body


def test_real_school_review_creates_exact_near_obligation(school_case):
    db, client, headers, sid, baseline, near = school_case[:6]
    _, scored, _, path, body = answer_and_review(school_case)
    with db.connect() as c:
        from home_study_agent.school_open_reviews import _anchor, _target
        now = datetime.now(timezone.utc)
        anchor = _anchor(c, CHILD, scored['attempt']['evidence_id'], now)
        _target(c, CHILD, anchor, near['diagnostic_item_id'], 'school_open_near', now, now)
        tasks = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (scored['attempt']['evidence_id'],)).fetchall()
        assert len(tasks) == 1, 'A real reviewed school original has no independent review obligation'
        assert tasks[0]['diagnostic_item_id'] == near['diagnostic_item_id']
        assert tasks[0]['interval_label'] == 'school_open_near'
        assert tasks[0]['status'] == 'pending'
    assert client.post(path, headers=headers, json=body).status_code == 200
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1


def next_day(case, monkeypatch, *, key, review_id=None, execute=True):
    from test_daily_learning_voice_integration import set_state
    from test_voice_runtime_binding import capture, interpret
    from test_cloud_first_agent import Planner
    db, client, headers, old_sid, baseline, near, far, supply, settings = case
    with db.connect() as c:
        review = dict(c.execute("SELECT * FROM review_task WHERE child_id=? AND status='pending'" +
            (' AND review_task_id=?' if review_id else '') + ' ORDER BY due_at DESC LIMIT 1',
            (CHILD,review_id) if review_id else (CHILD,)).fetchone())
    set_state(client, old_sid, 'reflection')
    closed = client.post(f'/v1/sessions/{old_sid}/reflection/complete', json={
        'learned_text': '', 'difficult_text': '', 'client_action_id': key+'-close'})
    assert closed.status_code == 200, closed.text
    # Pinned daytime preserves the real family end-of-day limit.
    from zoneinfo import ZoneInfo
    due = datetime.fromisoformat(review['due_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    next_time = (due + timedelta(days=1)).replace(hour=16, minute=0, second=0).astimezone(timezone.utc)
    clock = advance_clock(monkeypatch,next_time)
    unlocked = client.post('/v1/parent/unlock',json={'pin':'8264'})
    assert unlocked.status_code==200,unlocked.text
    headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
    started = client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':key+'-session'})
    assert started.status_code==201,started.text
    sid = started.json()['session_id']
    set_state(client,sid,'homework_capture')
    with db.connect() as c:
        c.execute('UPDATE daily_session SET homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?',(sid,))
        c.execute('UPDATE family_rule SET max_learning_minutes=90 WHERE child_id=?',(CHILD,))
        c.commit()
    plan = client.post(f'/v1/sessions/{sid}/plan/generate',json={'client_action_id':key+'-plan'})
    assert plan.status_code==200,plan.text
    slots = [r for r in plan.json()['plan_items'] if r['source_type']=='due_review' and r['source_ref']==review['review_task_id']]
    assert len(slots)==1,plan.json()['constraint_decisions']
    slot=slots[0]
    set_state(client,sid,'study')
    assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True}).status_code==200
    voice=capture(client,settings,monkeypatch,sid,'开始今天的复习任务',key=key+'-voice',child=CHILD)
    turn=interpret(client,sid,'开始今天的复习任务',key+'-command',voice)
    assert turn['runtime_capability']=='task.start',turn
    if not execute:
        return (db,client,headers,sid,baseline,near,far,supply,settings),slot,None,turn,clock
    executed=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert executed.status_code==200,executed.text
    result=executed.json()['execution']['result']
    assert result['status']=='active',result
    p=result['diagnostic_prompt']['prompt']
    assert p['review_task_id']==review['review_task_id'] and p['diagnostic_item_id']==review['diagnostic_item_id']
    return (db,client,headers,sid,baseline,near,far,supply,settings),slot,p,turn,clock


@pytest.mark.parametrize('score',[0,3])
def test_review_to_next_day_real_voice_prompt_original_review_complete(school_case,monkeypatch,score):
    from home_study_agent.repository import PlanInputError
    answer_and_review(school_case)
    case,slot,p,turn,clock=next_day(school_case,monkeypatch,key='near')
    db,client,headers,sid=case[:4]
    repo=StudyRepository(db)
    with pytest.raises(PlanInputError):
        repo.complete_plan_item(slot['plan_item_id'],1,'empty-completion')
    _,scored,assessment,path,body=answer_and_review(case,prompt=p,score=score,key='near',submit=False)
    with pytest.raises(PlanInputError):
        repo.complete_plan_item(slot['plan_item_id'],1,'reference-completion')
    reviewed=client.post(path,headers=headers,json=body)
    assert reviewed.status_code==200,reviewed.text
    result=repo.complete_plan_item(slot['plan_item_id'],1,'reviewed-completion')
    assert result['status']=='completed'
    with db.connect() as c:
        task=c.execute('SELECT * FROM review_task WHERE review_task_id=?',(slot['source_ref'],)).fetchone()
        assert task['result_evidence_id']==scored['attempt']['evidence_id'] and task['status']=='completed'
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',(scored['attempt']['attempt_id'],)).fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(slot['plan_item_id'],)).fetchone()[0]==1
        if score==3:
            far=c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='school_open_far'",(scored['attempt']['evidence_id'],)).fetchone()
            assert far and far['diagnostic_item_id']==case[6]['diagnostic_item_id']
    from home_study_agent.database import Database
    assert StudyRepository(Database(db.path)).complete_plan_item(slot['plan_item_id'],1,'reviewed-completion')['status']=='completed'


def test_retract_cancels_only_owned_obligation_and_does_not_revive_on_regrant(school_case):
    db,client,headers=school_case[:3]
    _,scored,assessment,path,body=answer_and_review(school_case)
    with db.connect() as c:
        before=dict(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(scored['attempt']['evidence_id'],)).fetchone())
    response=client.post(path,headers=headers,json={'operation':'retract','expected_revision':1,
        'client_action_id':'retract','reason':'Original review withdrawn for checking'})
    assert response.status_code==200,response.text
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    from home_study_agent.database import Database
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(before['review_task_id'],)).fetchone()[0]=='cancelled'
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c,'child-primary-4')['created']==[]
        c.rollback()
    cancelled={**before,'status':'cancelled'}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c,CHILD)['created']==[]
        assert dict(c.execute('SELECT * FROM review_task').fetchone())==cancelled
        c.rollback()
    regrant={**body,'expected_revision':2,'client_action_id':'regrant'}
    response=client.post(path,headers=headers,json=regrant)
    assert response.status_code==200,response.text
    with db.connect() as c:
        tasks=[dict(r) for r in c.execute('SELECT * FROM review_task ORDER BY rowid')]
    assert len(tasks)==2
    assert tasks[0]==cancelled
    successor=tasks[1]
    assert successor['status']=='pending'
    assert successor['predecessor_review_task_id']==before['review_task_id']
    assert successor['score_status']=='reviewed_human'
    assert successor['score_authority_id']!=before['score_authority_id']
    for field in ('child_id','source_evidence_id','knowledge_node_id','diagnostic_item_id','interval_label'):
        assert successor[field]==before[field]
    replay=client.post(path,headers=headers,json=regrant)
    assert replay.status_code==200,replay.text
    assert replay.json()['event']==response.json()['event']
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM review_task ORDER BY rowid')]==tasks


def test_reapproval_successor_fault_keeps_cancelled_root_and_retries_once(school_case):
    import sqlite3
    db,client,headers=school_case[:3]
    _,_,assessment,path,body=answer_and_review(school_case)
    response=client.post(path,headers=headers,json={'operation':'retract','expected_revision':1,
        'client_action_id':'successor-retract','reason':'Original review withdrawn for checking'})
    assert response.status_code==200,response.text
    with db.connect() as c:
        root=dict(c.execute('SELECT * FROM review_task').fetchone())
        assert root['status']=='cancelled'
        c.execute("""CREATE TRIGGER fail_successor BEFORE INSERT ON review_task
            WHEN NEW.predecessor_review_task_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'successor storage fault'); END""")
        c.commit()
    revised={**body,'expected_revision':2,'client_action_id':'successor-retry'}
    with pytest.raises(sqlite3.IntegrityError,match='successor storage fault'):
        client.post(path,headers=headers,json=revised)
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM review_task')]==[root]
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',
            (assessment['assessment_id'],)).fetchone()[0]==2
        c.execute('DROP TRIGGER fail_successor')
        c.commit()
    response=client.post(path,headers=headers,json=revised)
    assert response.status_code==200,response.text
    replay=client.post(path,headers=headers,json=revised)
    assert replay.status_code==200,replay.text
    assert replay.json()['event']==response.json()['event']
    with db.connect() as c:
        tasks=[dict(r) for r in c.execute('SELECT * FROM review_task ORDER BY rowid')]
        assert len(tasks)==2 and tasks[0]==root
        assert tasks[1]['status']=='pending'
        assert tasks[1]['predecessor_review_task_id']==root['review_task_id']
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',
            (assessment['assessment_id'],)).fetchone()[0]==3


def test_review_task_write_fault_rolls_back_parent_review_then_same_key_recovers(school_case):
    import sqlite3
    db,client,headers=school_case[:3]
    _,_,assessment,path,body=answer_and_review(school_case,submit=False)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_school_review BEFORE INSERT ON review_task BEGIN SELECT RAISE(ABORT,'school review storage fault'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='school review storage fault'):
        client.post(path,headers=headers,json=body)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',(assessment['assessment_id'],)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0]==0
        c.execute('DROP TRIGGER fail_school_review')
        c.commit()
    assert client.post(path,headers=headers,json=body).status_code==200


def test_near_far_delayed_real_chain_persists_and_retract_invalidates_descendants(school_case,monkeypatch):
    from home_study_agent.school_open_reviews import validate_review_source
    from home_study_agent.school_review_execution import validate_completion
    first=answer_and_review(school_case,score=3)
    case=school_case
    saved=[]
    for stage in ('near','far','retention'):
        case,slot,p,turn,clock=next_day(case,monkeypatch,key=stage)
        db,client,headers,sid=case[:4]
        assert p['diagnostic_item_id']==(case[5]['diagnostic_item_id'] if stage=='near' else case[6]['diagnostic_item_id'])
        submitted=answer_and_review(case,prompt=p,key=stage,score=3)
        done=client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={'actual_minutes':1,'client_action_id':stage+'-done'})
        assert done.status_code==200,done.text
        with db.connect() as c:
            row=dict(c.execute('SELECT * FROM review_task WHERE review_task_id=?',(slot['source_ref'],)).fetchone())
            assert row['status']=='completed' and row['result_evidence_id']==submitted[1]['attempt']['evidence_id']
            saved.append(row)
        reopened=client.school_reopen()
        headers={'X-HSC-Parent':reopened.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']}
        case=(db,reopened,headers,sid,*case[4:])
        replay=reopened.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={'actual_minutes':1,'client_action_id':stage+'-done'})
        assert replay.status_code==200 and replay.json()['status']=='completed',replay.text
        with db.connect() as c:
            assert [dict(r) for r in c.execute('SELECT * FROM review_task WHERE status=\'completed\' ORDER BY due_at')]==saved
    assert (datetime.fromisoformat(saved[2]['due_at'])-datetime.fromisoformat(saved[1]['completed_at']))>=timedelta(days=7)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE status='pending'").fetchone()[0]==0
    retract=case[1].post(first[3],headers=case[2],json={'operation':'retract','expected_revision':1,
        'client_action_id':'withdraw-baseline','reason':'Original assessment source requires checking'})
    assert retract.status_code==200,retract.text
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM review_task ORDER BY due_at')]==saved
        with pytest.raises(ValueError):
            validate_review_source(c,CHILD,saved[-1])
        with pytest.raises(ValueError):
            validate_review_source(c,'child-primary-4',saved[-1])


@pytest.mark.parametrize('fault',['cancel','source','same_item_other_prompt','plan_version'])
def test_scheduled_review_exact_prompt_source_and_cancel_guard(school_case,monkeypatch,fault):
    from home_study_agent.repository import PlanInputError
    answer_and_review(school_case)
    case,slot,p,turn,_=next_day(school_case,monkeypatch,key='guard')
    db,client,headers,sid=case[:4]
    if fault=='same_item_other_prompt':
        cancelled=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel',json={
            'prompt_id':p['prompt_id'],'item_version':p['item_version']})
        assert cancelled.status_code==200,cancelled.text
        alternate=DiagnosticPromptService(db).prepare(sid,track='school',preferred_item_id=p['diagnostic_item_id'],client_action_id='other-prompt')['prompt']
        answer_and_review(case,prompt=alternate,key='other',score=3)
        with pytest.raises(PlanInputError):
            StudyRepository(db).complete_plan_item(slot['plan_item_id'],1,'wrong-complete')
        return
    with db.connect() as c:
        if fault=='cancel':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
        elif fault=='source':
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?",(p['diagnostic_item_id'],))
        else:
            c.execute("UPDATE plan_constraint_decision SET metadata_json=json_set(metadata_json,'$.planning_evidence.school_review_binding.item_version','changed') WHERE source_ref=?",(slot['source_ref'],))
        c.commit()
    read=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':p['prompt_id']})
    assert read.status_code==200 and read.json()['status']=='invalidated',read.text
    rejected=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
        'prompt_id':p['prompt_id'],'item_version':p['item_version'],'answer':'late response','client_action_id':'late-answer'})
    assert rejected.status_code==200 and rejected.json()['attempt'] is None,rejected.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?',(slot['source_ref'],)).fetchone()[0]==0


def test_no_unseen_near_source_retains_explicit_supply_gap_across_restart(school_case):
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    from home_study_agent.database import Database
    db,client,headers,sid,baseline,near=school_case[:6]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?",(near['diagnostic_item_id'],))
        c.commit()
    _,scored,*_=answer_and_review(school_case)
    snapshots=[]
    for database in (db,Database(db.path)):
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            state=reconcile_school_reviews(c,CHILD)
            snapshots.append(state['blocked'])
            assert state['created']==[]
            assert len(state['blocked'])==1 and state['blocked'][0]['source_evidence_id']==scored['attempt']['evidence_id']
            assert state['blocked'][0]['stage']=='near_transfer'
            c.commit()
            assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0]==0
    assert snapshots[0]==snapshots[1]

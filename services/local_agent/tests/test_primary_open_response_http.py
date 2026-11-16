"""Real loopback HTTP/SQLite; authored questions, synthetic planner and native text.

No model transport, real-child scoring claim, formal library or generated calibration.
"""
import json
import base64
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import httpx
import pytest
import uvicorn

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.open_response_source import SCHEMA, VALIDATOR
from home_study_agent.writing_rubric import prepare_writing_review
from test_cloud_first_agent import Planner


class PrimaryGateway(Planner):
    async def complete(self, request):
        if request.request_type == 'learning_outcome_evaluation':
            from test_paper_writing_runtime_http import PaperGateway
            self.evaluations = getattr(self, 'evaluations', [])
            return await PaperGateway.complete(self, request)
        return await super().complete(request)

CHILD = 'child-primary-4'
CASES = {
    'math-reasoning': ('数学', 'mathematical_process', [
        '4盒彩笔，每盒10支，平均分给5人。解释4×10÷5中每一步的意义，并检查结果。',
        '6袋糖，每袋8颗，平均分给4人。解释先算什么、再算什么，并用原条件检查。',
        '每人得到9颗糖，一共5人。原来有3袋且每袋一样多，说明怎样求每袋颗数并检查。'],
        '我先求一共有多少，再平均分。因为每袋一样多，所以可以先乘后除，最后乘回去检查。'),
    'chinese-reading': ('语文', 'textual_interpretation', [
        '读短文：小明看见天空飘来乌云，把晒在院里的衣服收进屋。小明为什么收衣服？结合原文说明依据。',
        '读短文：小兰看见花盆的土已经干了，拿来一壶水浇花。她为什么拿水？结合原文说明依据。',
        '读短文：门外传来妹妹的喊声，小华放下书去开门。从哪一处能看出小华可能在等家人？说明推断的依据和边界。'],
        '原文说小明看到乌云，又把衣服收进屋。我猜他担心下雨打湿衣服，但原文没有说后来真的下雨。'),
    'chinese-writing': ('语文', 'communicative_writing', [
        '给同学介绍一次你熟悉的课间活动，写清事情的顺序、一个具体细节和你的感受。',
        '向家人介绍一次你熟悉的班级劳动，选择具体细节，按顺序说明经过和感受。',
        '为新同学写一段校园活动介绍，选择合适细节，让对方知道活动怎样进行以及你喜欢的原因。'],
        '课间我们一起跳绳。我先帮同学摇绳，绳子碰地时发出轻轻的响声。后来我也跳了三下，觉得很开心。'),
    'english-reading': ('英语', 'textual_interpretation', [
        'Read: Tom takes a ball to the park. Sam is waiting there. They like playing football together. Why does Tom take a ball? Give a clue from the text.',
        'Read: Amy takes a pencil to school. Today her class will draw a picture. Why does Amy need the pencil? Explain using the text.',
        'Read: Ben brings two cups to the table. His friend is coming for juice. What can you infer about the cups? Explain what the text does and does not say.'],
        'Tom takes the ball because he and Sam like playing football together. The text says Sam is waiting in the park.'),
}


def post(client, path, body):
    result = client.post(path, json=body)
    assert result.status_code == 200, (path, result.status_code, result.text[:1400])
    return result.json()


@contextmanager
def tcp(settings, gateway):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    application = create_app(settings, model_gateway=gateway)
    server = uvicorn.Server(uvicorn.Config(application,
        log_level='error', access_log=False, lifespan='on'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    statistics={'requests':0,'responses_500':0,'max_request_ms':0.0,'owned_threads_after_close':None}
    def requested(request):
        request.extensions['primary_started']=time.perf_counter()
    def received(response):
        response.read()
        statistics['requests']+=1
        statistics['responses_500']+=int(response.status_code>=500)
        statistics['max_request_ms']=max(statistics['max_request_ms'],
            round((time.perf_counter()-response.request.extensions['primary_started'])*1000,2))
    try:
        deadline = time.monotonic()+60
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.02)
        assert server.started, 'Owned TCP backend failed to start'
        with httpx.Client(base_url='http://127.0.0.1:'+str(sock.getsockname()[1]),
                headers={'X-HSC-Session':settings.session_token}, timeout=60, trust_env=False,
                event_hooks={'request':[requested],'response':[received]}) as client:
            client.app = application
            client.tcp_statistics=statistics
            yield client
    finally:
        server.should_exit = True
        thread.join(60)
        sock.close()
        statistics['owned_threads_after_close']=int(thread.is_alive())
        assert not thread.is_alive(), 'Owned TCP backend did not drain'


def prepared(tmp_path, slug, *, response_mode='written'):
    subject, family, questions, answer = CASES[slug]
    db = Database(tmp_path/'primary.db')
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    source = dict(schema=SCHEMA,subject=subject,grade='四年级',knowledge_node_id='node-p4-open-'+slug,
        answer_type='open_response',assessment_mode='open_response_rubric',task_family=family,response_mode=response_mode,
        task_points=['回应本题的问题','使用本题的具体信息说明理由或表达细节'],context_requirement='self_contained')
    imported = KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',
        title='Synthetic primary authored questions',subject=subject,grade_band='四年级',version_label='isolated-primary-v1',
        source_ref='isolated:primary-open',license_status='public',parent_reviewed=True,
        content_text='\n'.join(json.dumps(dict(source,prompt=p),ensure_ascii=False) for p in questions)))
    quote = '根据具体任务的条件和材料，用可追溯的信息说明理解和表达，不据此推定完整学科掌握。'
    with db.connect() as c:
        excerpts = [r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (imported['source_id'],))]
        c.execute('''INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,subject_title,title,
            version_label,official_url,local_root_path,status,imported_at,updated_at)
            VALUES('primary-standard','isolated-primary','CN','national_standard',?,'Synthetic standard fixture','2022',
            'isolated:primary-standard','','structured','2026-09-01','2026-09-01')''',(subject,))
        c.execute("INSERT INTO curriculum_standard_page VALUES('primary-standard',12,?,'fixture-hash',20)",(quote,))
        c.execute('''INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,
            requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('primary-requirement','primary-standard',?,'第二学段','content_requirement',?,?,'Traceable explanation',12,12,.9,
            'verified','2026-09-01','2026-09-01')''',(subject,quote,quote))
        c.commit()
    CurriculumSupplyService(db).select_curriculum(CHILD,dict(grade='四年级',subject=subject,edition='isolated-primary-v1',
        semester='上学期',standard_scope='第二学段',textbook_source_id=imported['source_id']),
        actor='parent',action_id='primary-scope',expected_version=0)
    specs = [dict(seed_item_id='diag-p4-open-'+slug+'-1',stage=stage,excerpt_id=excerpt,
        requirement_id='primary-requirement',standard_page=12,standard_quote=quote,validator=VALIDATOR,
        explanation='Synthetic reference source only; no keyword scoring or automatic mastery.',difficulty=2)
        for stage,excerpt in zip(('parallel','near_transfer','far_transfer'),excerpts)]
    return db, specs, source, imported['source_id'], answer


def publish(client, spec):
    base = '/api/parent/children/'+CHILD+'/curriculum-supply'
    stage = spec['stage']
    body = dict(action_id=stage+'-draft',expected_version=0,expected_scope_version=1,source=spec)
    draft = post(client,base+'/generate',body)
    assert post(client,base+'/generate',body)['candidate_id']==draft['candidate_id']
    path = base+'/'+draft['candidate_id']
    checked = post(client,path+'/validate',dict(action_id=stage+'-validate',expected_version=1))
    review = dict(action_id=stage+'-review',expected_version=2,decision='approve',note='Synthetic original source and task criteria checked')
    if stage != 'parallel':
        context = checked['stage_review_context']
        review['stage_review'] = {k:context[k] for k in ('schema','binding_version')}
        review['stage_review'].update(stage=stage,source_quote=context['source_prompt'],candidate_quote=context['candidate_prompt'],
            same_skill_rationale='Both tasks require the same source-bound explanation or detailed expression.',
            changed_context_rationale='The subject matter and conditions change; the later task needs a new response, not a repeated answer.')
    post(client,path+'/review',review)
    result = post(client,path+'/publish',dict(action_id=stage+'-publish',expected_version=3))
    assert result['eligible'] and result['state']=='published'
    return result


def grant_question(client, item_id, key, revision=0):
    from test_writing_question_authorization import LICENSE
    path='/v1/parent/children/'+CHILD+'/writing-question-authorizations/'+item_id
    authorization=client.get(path).json()
    return post(client,path+'/grant',dict(**{k:authorization[k] for k in ('item_version','source_version','curriculum_version')},
        expected_revision=revision,client_action_id=key,license_basis=LICENSE))


def answer_review(db, client, sid, prompt, text, key, *, submit=True):
    original_body = dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],text=text,client_action_id=key+'-original')
    original = WritingOriginalService(db).capture_native_response(sid,**original_body)
    assert WritingOriginalService(Database(db.path)).capture_native_response(sid,**original_body)['artifact_id']==original['artifact_id']
    body = dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=text,
        writing_original_artifact_id=original['artifact_id'],client_action_id=key+'-answer')
    path = '/v1/sessions/'+sid+'/diagnostic-prompt/answer'
    scored = post(client,path,body)
    assert post(client,path,body)['attempt']['attempt_id']==scored['attempt']['attempt_id']
    assert scored['attempt']['correctness'] is None
    follow = scored['local_learning_evaluation']
    assert follow['status']=='feedback_required', follow
    evaluated = post(client,'/v1/agent-turns/'+follow['feedback_turn_id']+'/execute',{})
    assessment = evaluated['execution']['result']['rubric_assessment']
    review_path, review = review_assessment(db, client, assessment, text, key, submit=submit)
    return scored, assessment, review_path, review, body


def review_assessment(db, client, assessment, text, key, *, submit):
    detail_path='/v1/parent/children/'+CHILD+'/learning-rubrics/'+assessment['assessment_id']
    unreviewed=client.get(detail_path).json()
    assert not unreviewed['qualification']['score_qualified']
    assert not unreviewed['qualification']['eligible_for_mastery_gate']
    with db.connect() as c:
        binding = prepare_writing_review(c,CHILD,assessment['assessment_id'])
    review = dict(expected_revision=0,client_action_id=key+'-review',operation='approve',
        reason='Synthetic low-score parent review; not real child or model quality evidence.',independent=True,hint_level=0,
        review_profile='school_open_written_v1',writing_evidence={'binding_version':binding['binding_version'],
            'task_point_evidence':[{'task_point':p,'response_quote':text} for p in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'],score=0,response_quotes=[text],rationale='Synthetic low-score review') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=p,response_quote=text) for p in assessment['criteria']])
    review_path='/v1/parent/children/'+CHILD+'/learning-rubrics/'+assessment['assessment_id']+'/review'
    if submit:
        result=post(client,review_path,review)
        assert result['qualification']['score_qualified']
        assert not result['qualification']['eligible_for_mastery_gate']
    return review_path, review


def paper_answer(db, client, gateway, settings, monkeypatch, sid, prompt):
    from test_paper_writing_runtime_http import PaperGateway, synthetic_page
    from test_voice_runtime_binding import capture, interpret
    from test_writing_voice_runtime_http import readback_receipt
    from home_study_agent.document_vision import DocumentVisionService, DocumentVisionError
    def no_image_cloud(*args, **kwargs):
        raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_image_cloud)
    png,text=synthetic_page(monkeypatch)
    base='/v1/sessions/'+sid+'/document-monitor'
    post(client,base+'/start',{'client_action_id':'primary-monitor'})
    page=post(client,base+'/ocr',{'image_base64':base64.b64encode(png).decode(),'client_action_id':'primary-ocr'})
    post(client,base+'/revisions',{'artifact_id':page['artifact_id'],'content_signature':'synthetic-primary-sheet',
        'client_action_id':'primary-revision'})
    paper=PaperGateway()
    monkeypatch.setattr(gateway,'complete',paper.complete)
    def say(quote,key,control):
        paper.quote,paper.control=quote,control
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,key+'-voice')
        return interpret(client,sid,quote,key,voice)
    say('写完了，帮我看看过程','primary-look','writing.read_original')
    reading=say('这页只有我写的答案','primary-scope','writing.read_original')
    assert reading['action']['type']=='read_writing_original',reading
    playback=readback_receipt(client,sid,reading)
    outcome=client.put('/v1/agent-turns/'+reading['agent_turn_id']+'/outcome',json={
        'final_response':'原稿已读回','tool_status':'completed','result':playback})
    assert outcome.status_code==200,outcome.text
    confirmed=say('对，是我写的，识别没有错','primary-confirm','writing.confirm_original')
    path='/v1/agent-turns/'+confirmed['agent_turn_id']+'/execute'
    submitted=post(client,path,{})
    assert post(client,path,{})['turn']['local_evaluation']==submitted['turn']['local_evaluation']
    follow=submitted['turn']['local_evaluation']
    assert follow['status']=='feedback_required',json.dumps(follow,ensure_ascii=False)
    evaluated=post(client,'/v1/agent-turns/'+follow['feedback_turn_id']+'/execute',{})
    assessment=evaluated['execution']['result']['rubric_assessment']
    with db.connect() as c:
        attempt=dict(c.execute('SELECT * FROM diagnostic_attempt WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone())
    assert attempt['submitted_answer']==text and attempt['correctness'] is None
    path,body=review_assessment(db,client,assessment,text,'next',submit=False)
    return {'attempt':attempt},assessment,path,body


@pytest.mark.parametrize('slug', list(CASES))
def test_primary_tcp_original_review_next_day_retest_and_restart(tmp_path, monkeypatch, slug):
    from test_school_open_reviews import next_day
    from test_daily_learning_voice_integration import set_state
    import home_study_agent.paper_writing_projection
    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH',str(tmp_path/'primary.db'))
    monkeypatch.setattr('test_school_open_reviews.CHILD',CHILD)
    db, specs, source, source_id, answer = prepared(tmp_path,slug)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        session_token='isolated-primary-tcp',data_encryption_key=b'P'*32)
    quote='我想做一道题，把我的思考写下来'
    gateway=PrimaryGateway('diagnostic.start',{},quote,objective_type='learning')
    gateway.payload['objective'].update(phase='diagnose',success_criteria=source['task_points'])
    transports=[]
    with tcp(settings,gateway) as client:
        transports.append(client.tcp_statistics)
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/setup',{'pin':'8264'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':True}).status_code==200
        baseline,near,far=[publish(client,spec) for spec in specs]
        grant_question(client,baseline['diagnostic_item_id'],'baseline-grant')
        start=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'primary-session'})
        assert start.status_code==201,start.text
        sid=start.json()['session_id']
        set_state(client,sid,'study')
        turn=post(client,'/v1/sessions/'+sid+'/agent-turn',{'transcript':quote,'client_action_id':'primary-start'})
        assert turn['runtime_capability']=='diagnostic.start',turn
        ready=post(client,'/v1/sessions/'+sid+'/diagnostic-prompt/prepare',dict(track='school',subject=source['subject'],
            preferred_item_id=baseline['diagnostic_item_id'],client_action_id='primary-prompt'))
        p=ready['prompt']
        outcome=client.put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome',json={'tool_status':'completed',
            'final_response':'原题已返回，等待原始作答。','result':{'execution_receipt':{
                'capability':'diagnostic.start','execution_location':'client_device_bridge','action_type':'start_diagnostic',
                'status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'output':ready}}})
        assert outcome.status_code==200,outcome.text
        scored,assessment,path,body,answer_body=answer_review(db,client,sid,p,answer,'baseline')
        with db.connect() as c:
            task=dict(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(scored['attempt']['evidence_id'],)).fetchone())
            assert task['diagnostic_item_id']==near['diagnostic_item_id'] and task['status']=='pending'
            review_at=c.execute('SELECT created_at FROM rubric_review_event WHERE assessment_id=?',(assessment['assessment_id'],)).fetchone()[0]
            assert datetime.fromisoformat(task['due_at'])-datetime.fromisoformat(review_at)==timedelta(days=1)
        other=path.replace(CHILD,'child-junior-3')
        denied=client.post(other,json=body)
        assert denied.status_code==409 and denied.json()['detail']['code']=='RUBRIC_ASSESSMENT_NOT_OWNED'
        post(client,path,body)
        from home_study_agent.writing_score import current_written_score
        from home_study_agent.learning_rubric import RubricEvidenceService
        with db.connect() as c:
            original_row=dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(scored['attempt']['attempt_id'],)).fetchone())
            for sql,args in (
                ("UPDATE knowledge_source SET child_id='child-junior-3' WHERE source_id=?",(source_id,)),
                ("UPDATE knowledge_source SET review_status='pending' WHERE source_id=?",(source_id,)),
                ("UPDATE knowledge_excerpt SET content_text=content_text||' changed' WHERE excerpt_id=?",(specs[0]['excerpt_id'],)),
                ("UPDATE capture_artifact SET status='withdrawn' WHERE child_id=? AND artifact_type='writing_original'",(CHILD,)),
            ):
                c.execute('BEGIN IMMEDIATE')
                c.execute(sql,args)
                with pytest.raises(ValueError):
                    current_written_score(c,CHILD,assessment['assessment_id'],now=datetime.now(timezone.utc))
                c.rollback()
                assert RubricEvidenceService(None).qualification(CHILD,assessment['assessment_id'],connection=c)['score_qualified']
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(original_row['attempt_id'],)).fetchone())==original_row
        gateway.payload=Planner('task.start',{'start_selector':{
            'subject':None,'task_kind':'review','source_type':None,
            'target_quote':'开始今天的复习任务'}},'开始今天的复习任务').payload
        case=(db,client,{'X-HSC-Parent':client.headers['X-HSC-Parent']},sid,baseline,near,far,CurriculumSupplyService(db),settings)
        from test_writing_stage_review_consumers import advance_clock
        # Exercise the actual next-day due instant, not the helper's extra day.
        monkeypatch.setattr('test_school_open_reviews.advance_clock',
            lambda patch, unused: advance_clock(patch,datetime.fromisoformat(task['due_at'])+timedelta(minutes=1)))
        case,slot,p2,_,clock=next_day(case,monkeypatch,key='primary-next')
        client.headers.update(case[2])
        sid2=case[3]
        grant_question(client,near['diagnostic_item_id'],'near-grant')
        follow,second,review_path,review_body=paper_answer(db,client,gateway,settings,monkeypatch,sid2,p2)
        from home_study_agent.repository import PlanInputError
        with pytest.raises(PlanInputError):
            StudyRepository(db).complete_plan_item(slot['plan_item_id'],1,'reference-cannot-complete')
        post(client,review_path,review_body)
        completed=StudyRepository(db).complete_plan_item(slot['plan_item_id'],1,'primary-complete')
        assert completed['status']=='completed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(CHILD,)).fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=? AND correctness IS NOT NULL',(CHILD,)).fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM agent_outcome WHERE child_id=? AND outcome_level IN ('independent','transferred','retained')",(CHILD,)).fetchone()[0]==0
        original_id=scored['attempt']['attempt_id']
    with tcp(settings,gateway) as client:
        transports.append(client.tcp_statistics)
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/unlock',{'pin':'8264'})['parent_token']
        assert StudyRepository(Database(db.path)).complete_plan_item(slot['plan_item_id'],1,'primary-complete')['status']=='completed'
        with db.connect() as c:
            row=c.execute('SELECT submitted_answer,correctness FROM diagnostic_attempt WHERE attempt_id=?',(original_id,)).fetchone()
            assert row[0]==answer and row[1] is None
        post(client,review_path,dict(operation='retract',expected_revision=1,client_action_id='primary-retract',reason='Synthetic review revoked'))
        from home_study_agent.writing_score import current_written_score
        with db.connect() as c, pytest.raises(ValueError):
            current_written_score(c,CHILD,second['assessment_id'],now=clock.now(timezone.utc))
        withdraw='/api/parent/children/'+CHILD+'/curriculum-supply/'+baseline['candidate_id']+'/withdraw'
        post(client,withdraw,dict(action_id='source-withdraw',expected_version=4,note='Synthetic original source revoked'))
        from home_study_agent.local_supply import validate_item, LocalSupplyError
        with db.connect() as c, pytest.raises(LocalSupplyError):
            validate_item(c,CHILD,baseline['diagnostic_item_id'])
        with db.connect() as c:
            # Separate authored near source remains valid, but cannot revive the withdrawn anchor.
            assert validate_item(c,CHILD,near['diagnostic_item_id'])
            with pytest.raises(ValueError):
                current_written_score(c,CHILD,assessment['assessment_id'],now=clock.now(timezone.utc))
    assert all(row['owned_threads_after_close']==0 and row['responses_500']==0 for row in transports)
    (tmp_path/'TCP-RESULT.json').write_text(json.dumps(dict(domain=slug,child_id=CHILD,db=str(db.path),
        original_attempt_id=original_id,review_task_id=slot['source_ref'],
        primary_original_retained=True,secondary_original='synthetic OCR manuscript, deliberately unrelated',
        restart='two real TCP app lifespans in one Python interpreter',transports=transports,
        gateway='in-process synthetic planner and formative output',external_model_posts=0,
        low_score_qualified_before_retraction=True,exact_or_mastery_claims=0),ensure_ascii=False,indent=2)+'\n')

"""Real typed ASR/Agent/answer/formative HTTP; synthetic PCM, recognizer and model only."""
import json
from contextlib import ExitStack
import hashlib
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.model_gateway import ModelGateway
from test_diagnostic_prompt_http import start
from test_open_response_supply_http import CHILD, prepared, publish_open
from test_voice_runtime_binding import capture, interpret
from test_writing_question_authorization import LICENSE


def test_legacy_unversioned_answer_role_is_not_upgraded_to_submission_authority():
    from home_study_agent.voice_control_intent import VoiceControlIntent
    quote = '我口头说明这个结论。'
    key = hashlib.sha256(b'voice-control:old-request').hexdigest()
    fingerprint = hashlib.sha256(json.dumps(['child','session',quote],ensure_ascii=False).encode()).hexdigest()
    with sqlite3.connect(':memory:') as c:
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE voice_control_intent_request(request_key,child_id,session_id,status,fingerprint,decision_json,binding_json)')
        c.execute('INSERT INTO voice_control_intent_request VALUES(?,?,?,?,?,?,?)',
            (key,'child','session','completed',fingerprint,json.dumps({'status':'non_control','role':'answer'}),'{}'))
        assert VoiceControlIntent.spoken_answer_target(c,'session','child','old-request',quote,key) is None
    parsed = VoiceControlIntent.parse(json.dumps({'role':'answer','control':None,'quote':quote,'confidence':.99}),quote)
    assert parsed['role_version'] == 'spoken_answer_role_v1'


def spoken_source(tmp_path, subject):
    db, supply, spec, source, _ = prepared(tmp_path, subject, license_status='public')
    source = {**source, 'response_mode':'spoken_explanation',
        'prompt':source['prompt'].replace('写出', '口头说明')}
    imported = KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',title='Isolated oral reasoning task',
        subject=subject,grade_band='初三',version_label='isolated-open-2026',source_ref='isolated-spoken-source',
        license_status='public',parent_reviewed=True,content_text=json.dumps(source,ensure_ascii=False)))
    with db.connect() as c:
        scope = json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection WHERE child_id=?',(CHILD,)).fetchone()[0])
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(imported['source_id'],)).fetchone()[0]
    supply.select_curriculum(CHILD,{**scope,'textbook_source_id':imported['source_id']},actor='parent',action_id='spoken-scope',expected_version=1)
    published = publish_open(supply,{**spec,'excerpt_id':excerpt})
    return db, source, published


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_reviewed_oral_source_is_available_for_goal_practice_not_a_learner_score(tmp_path, subject):
    from home_study_agent.database import Database
    from home_study_agent.goal_question_inventory import practice_item
    from home_study_agent.curriculum_supply import CurriculumSupplyService
    from home_study_agent.learning_obligations import project_child_mastery_evidence
    db, source, published = spoken_source(tmp_path, subject)
    item_id = published['diagnostic_item_id']
    with Database(db.path).connect() as c:
        item = practice_item(c, CHILD, item_id, datetime.now(timezone.utc))
        assert item is not None, 'Reviewed oral reasoning must not be filtered out as unscorable text'
        assert item['prompt'] == source['prompt']
        assert json.loads(item['expected_answer_json'])['response_mode'] == 'spoken_explanation'
        assert practice_item(c, 'child-primary-4', item_id, datetime.now(timezone.utc)) is None
        assert not project_child_mastery_evidence(c, CHILD)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    withdrawn = CurriculumSupplyService(db).withdraw(CHILD, published['candidate_id'], actor='parent',
        action_id='oral-inventory-withdraw', expected_version=4, note='Synthetic source withdrawal')
    assert not withdrawn['eligible']
    with Database(db.path).connect() as c:
        assert practice_item(c, CHILD, item_id, datetime.now(timezone.utc)) is None


@pytest.mark.parametrize('subject,role_fault,goal_start,late_fault', [
    ('authored:数学',None,True,None),('authored:语文',None,False,None),
    ('authored:英语',None,False,None),('authored:物理',None,False,None),
    ('authored:化学',None,False,None),('authored:历史',None,False,None),
    ('authored:道德与法治',None,False,None),('authored-p4:数学',None,False,None),
    ('authored-p4:语文',None,False,None),('authored-p4:英语',None,False,None),
    ('authored:英语','cloud_disabled',False,None),
    ('数学','source_overlap',False,None),
    ('数学',None,False,None),('物理',None,False,None),
    ('数学','low_confidence',False,None),('数学','rewritten_quote',False,None),('物理','withdrawn',False,None),
    ('数学',None,True,None),('数学',None,True,'budget'),('数学',None,True,'receipt'),
    ('数学',None,True,'source')])
def test_natural_spoken_process_asr_bound_answer_formative_and_restart(tmp_path,monkeypatch,subject,role_fault,goal_start,late_fault,continuation=None,followup_fault=None,help_request=None,help_fault=None,raw_mode=None,question_authorized=True,after_saved=None,probe_case=None,grader_reply=None,http_client_address=None):
    authored = subject.startswith('authored')
    CHILD = 'child-primary-4' if subject.startswith('authored-p4:') else 'child-junior-3'
    if authored:
        from test_school_oral_catalog import install
        from home_study_agent.school_oral_catalog import TASKS, items
        grade = '四年级' if CHILD == 'child-primary-4' else '初三'
        subject = subject.partition(':')[2]
        db = install(tmp_path / 'authored-http.db')
        item = min((row for task,row in zip(TASKS,items()) if task[2:4] == (subject,grade)
                    and (probe_case is None or task[0]==probe_case['task_slug'])), key=lambda row:row[0])
        source = {'prompt':item[2], 'task_points':item[4]['task_points'], 'knowledge_node_id':item[1]}
        published = {'diagnostic_item_id':item[0]}
    else:
        db, source, published = spoken_source(tmp_path,subject)
        # This branch tests one imported source and its withdrawal, independently
        # of shipped alternatives. The authored cases above use the full catalog.
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id LIKE 'diag-authored-oral-v1-%'")
            c.commit()
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        model_provider='qwen',model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f',model_name='isolated-model',model_base_url='https://voice.invalid/v1',
        tutor_explanation_api_key='synthetic-tutor-key-123',tutor_explanation_base_url='https://tutor.example.test/v1',tutor_explanation_model='deepseek-test',
        data_encryption_key=b'8' * 32)
    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    quote = ('我先算判别式，二的平方减四乘一乘五得到负十六，所以没有实数解。'
        if subject == '数学' else '我会让导体的材料和粗细保持一样，只改变长度，分别测量电压和电流再算电阻，比较结果。')
    if authored:
        # Deliberately incomplete, subject-relevant original; never a gold label.
        quote = {
            ('初三','数学'):'四减去四倍k不能小于零，所以k最多是一，等于一的时候两个根重合。',
            ('初三','语文'):'他没有盯着失败追问，还愿意陪我继续做桥。我觉得他在用行动鼓励我。',
            ('初三','英语'):'Only one entrance is shut. People can go through the other gate, and some visitors can ask for help.',
            ('初三','物理'):'这次连粗细也换了，不只是拉长。我会另取一样粗的那种线，温度也不能变。',
            ('初三','化学'):'改下标就变成另一种物质了。我先把氧凑成六个，再发现铝也要放四个。',
            ('初三','历史'):'钱不够已经很久，后来又遇到灾害，不能只算其中一个原因。我还想看方案到底有没有实行。',
            ('初三','道德与法治'):'不知道东西在哪不代表是他拿了。可以先翻借用记录，再问有没有人知道。',
            ('四年级','数学'):'花掉的其实是十八块，再分给三本，算出每本六块。找回的钱不能算进本子里。',
            ('四年级','语文'):'雨可能会弄湿那些画，她回去把画拿到里面，是想保护它们。',
            ('四年级','英语'):'It is by the door on a chair. Ben needs to take his cap when he goes to the park.',
        }[(grade,subject)]
    if probe_case:
        assert authored and probe_case['subject']==subject and probe_case['grade']==grade
        quote=probe_case['payload']['response']
    if role_fault == 'withdrawn':
        quote += '等一下，我还没有说完，先别提交。'
    if role_fault == 'source_overlap':
        quote = '我在想这道题：' + source['prompt']
    posts, evaluations = [], []
    command_state = {}
    tutor_calls = []
    tutor_mode = False
    start_quote = '我想做一道数学题，口头说明我的推理过程' if subject == '数学' else '我想练习口头解释一个物理实验'
    if authored:
        start_quote = f'我想练习{subject}，口头说明我的想法'
    if goal_start:
        start_quote = '按今天的安排开始这项推理练习'
    def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        if tutor_mode:
            assert request.url.host == 'tutor.example.test'  # The speaking-to-Tutor path must use the dedicated model.
            tutor_calls.append(body)
            if help_fault=='budget_during':
                with db.connect() as c:
                    c.execute('UPDATE plan_item SET expected_minutes=expected_minutes+1 WHERE plan_item_id=?',(goals[0]['plan_item_id'],))
                    c.commit()
            teaching='先只看实数根存在需要满足的条件：你会怎样判断判别式的正负？'
            if help_fault=='long_explanation':
                teaching=teaching*160
            return httpx.Response(200,json={'choices':[{'message':{'content':teaching}}]})
        if len(posts) == 1:
            from test_cloud_first_agent import Planner
            value = Planner('task.start' if goal_start else 'diagnostic.start',{},start_quote,
                            objective_type='execution' if goal_start else 'learning').payload
            value['objective'].update(phase='execute' if goal_start else 'diagnose',success_criteria=source['task_points'])
            value['requires_child_reply'] = True
            value['spoken_transition'] = '请对接下来的题目口头说明推理过程。'
        elif '只分类当前原话' in body['messages'][0]['content']:
            classified = json.loads(body['messages'][-1]['content'])['transcript']
            value = ({'role':'control','control':'task.start','quote':classified,'confidence':.99}
                     if goal_start and classified == start_quote else
                     {'role':'answer','control':None,'quote':classified,'confidence':.99})
            if command_state:
                declared = command_state['control'] in json.dumps(body,ensure_ascii=False)
                value = {'role':'control' if declared else 'unknown',
                    'control':command_state['control'] if declared else None,
                    'quote':command_state['quote'],'confidence':.99}
                if declared and command_state['control']=='task.resume':
                    value['selector']=command_state.get('selector',{'subject':None,'task_kind':None,'relative_order':None})
                if declared and command_state['control']=='task.start':
                    value['start_selector']=command_state['start_selector']
                if command_state['control']=='answer':
                    value={'role':'answer','control':None,'quote':command_state['quote'],'confidence':.99}
            if role_fault == 'low_confidence':
                value['confidence'] = .1
            elif role_fault == 'rewritten_quote':
                value['quote'] = '答案是没有实数解。'
        else:
            payload = json.loads(body['messages'][1]['content'])
            assert set(payload) == {'question','response','rubric'}
            evaluations.append(payload)
            if grader_reply is not None:
                # Replay prepaid synthetic API evidence, not a live provider or
                # a calibration credential. The application input must match.
                assert probe_case is not None and payload==probe_case['payload']
                return httpx.Response(200,json={
                    'choices':[{'finish_reason':grader_reply['finish_reason'],
                        'message':{'content':grader_reply['candidate']}}],
                    'usage':{key:grader_reply[key] for key in ('prompt_tokens','completion_tokens')}})
            value = {'understanding':'partial','confidence':.8,'evidence_summary':'Synthetic reference feedback, not mastery',
                'spoken_feedback':'你说明了一个推理步骤。再说明这个结论成立需要哪些条件。',
                'next_check':'用自己的话补充结论适用的条件。',
                'rubric_dimensions':[{'dimension_id':d['dimension_id'],'score':1,'response_quotes':[payload['response']],
                    'rationale':'Synthetic evaluation of the original spoken explanation'} for d in payload['rubric']['dimensions']],
                'criterion_evidence':[{'criterion':point,'response_quote':payload['response']} for point in payload['rubric']['task_points']]}
            from formative_response_fixture import criterion_reply
            value=criterion_reply(payload,value)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]})
    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    application = create_app(settings,model_gateway=gateway)
    with ExitStack() as lifecycle:
        client = lifecycle.enter_context(TestClient(application,raise_server_exceptions=False,
            **({'client':http_client_address} if http_client_address else {})))
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        assert client.put('/v1/parent/privacy',headers=headers,json={
            'cloud_model_text_allowed':True,'raw_media_storage_allowed':raw_mode!='never_store','cloud_audio_allowed':False}).status_code == 200
        if not authored and question_authorized:
            path = f"/v1/parent/children/{CHILD}/writing-question-authorizations/{published['diagnostic_item_id']}"
            initial = client.get(path,headers=headers).json()
            granted = client.post(path+'/grant',headers=headers,json={
                **{k:initial[k] for k in ('item_version','source_version','curriculum_version')},
                'expected_revision':0,'client_action_id':'spoken-question-grant','license_basis':LICENSE})
            assert granted.status_code == 200,granted.text
        if goal_start:
            from test_school_supply_resume_http import plan
            rule = client.put(f'/v1/parent/children/{CHILD}/family-rule', headers=headers, json=dict(
                latest_end_time=None,max_learning_minutes=90,task_segment_minutes=35,break_minutes=5,
                pet_min_minutes=0,competition_minutes=0,observation_mode='shadow',reminder_budget_per_10m=0))
            assert rule.status_code == 200, rule.text
            sid, workspace = plan(client)
            goals = [row for row in workspace['plan_items'] if row['source_type'] == 'junior_exam_goal']
            assert len(goals) == 1, workspace
            entered = client.post(f'/v1/sessions/{sid}/transition', json=dict(expected_state='plan_review',
                target_state='study',reason='Accept planned oral reasoning',client_action_id='oral-goal-study'))
            assert entered.status_code == 200, entered.text
        else:
            sid = start(client,CHILD)
            with db.connect() as c:
                c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
                c.commit()
        requested = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':start_quote,'client_action_id':'request-spoken-practice'})
        assert requested.status_code == 200,requested.text
        if goal_start:
            started = client.post(f"/v1/agent-turns/{requested.json()['agent_turn_id']}/execute")
            assert started.status_code == 200, started.text
            result = started.json()['execution']['result']
            assert result['plan_item_id'] == goals[0]['plan_item_id'], result
            prompt = result['diagnostic_prompt']['prompt']
            assert prompt['item']['diagnostic_item_id'] == published['diagnostic_item_id']
        else:
            assert requested.json().get('runtime_capability') == 'diagnostic.start',requested.text
            ready = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
                'track':'school','subject':subject,'preferred_item_id':published['diagnostic_item_id'],'client_action_id':'spoken-prompt'})
            assert ready.status_code == 200,ready.text
            prompt = ready.json()['prompt']
            # Synthetic bridge outcome; the goal branch uses actual execution.
            presented = client.put(f"/v1/agent-turns/{requested.json()['agent_turn_id']}/outcome",json={
                'tool_status':'completed','final_response':'原题已返回，等待孩子说明过程。','result':{'execution_receipt':{
                    'capability':'diagnostic.start','execution_location':'client_device_bridge','action_type':'start_diagnostic',
                    'status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'output':ready.json()}}})
            assert presented.status_code == 200,presented.text
        if role_fault == 'cloud_disabled':
            assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False}).status_code == 200
        voice = capture(client,settings,monkeypatch,sid,quote,prompt,'spoken-process-audio', child=CHILD)
        heard = interpret(client,sid,quote,'spoken-process-turn',voice)
        if role_fault:
            assert heard['action']['type'] == 'none',heard
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0] == 0
            assert not evaluations
            if role_fault in {'source_overlap','cloud_disabled'}:
                assert len(posts) == 1
            return
        if subject == '英语' and heard['intent'] == 'pending_answer_clarification':
            # Repeating source text is kept local. Confirm the role through a
            # new saved ASR turn, retaining the original answer/audio ownership.
            assert len(posts) == 1 and quote not in json.dumps(posts, ensure_ascii=False)
            pending = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert pending.status_code == 200, pending.text
            confirmation = '刚才那句就是我的答案'
            confirmed_voice = capture(client, settings, monkeypatch, sid, confirmation,
                prompt, 'spoken-process-role-audio', child=CHILD)
            heard = interpret(client, sid, confirmation, 'spoken-process-role-confirmation', confirmed_voice)
        assert heard['action']['type'] == 'submit_diagnostic_answer', (
            heard['intent'],heard['response_text'],heard['action'].get('source'),len(posts))
        assert heard['action']['source_voice_input_id'] == voice
        payload = {'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'answer':quote,
            'client_action_id':'spoken-process-answer','authorized_turn_id':heard['agent_turn_id']}
        if late_fault:
            from home_study_agent.agent_runtime import AgentRuntimeStore
            from home_study_agent.curriculum_supply import CurriculumSupplyService
            original_finalize = AgentRuntimeStore.finalize_diagnostic_evaluation
            def change_after_saved(runtime, current_session, evidence_id):
                if late_fault == 'source':
                    CurriculumSupplyService(db).withdraw(CHILD, published['candidate_id'], actor='parent',
                        action_id='late-oral-withdraw', expected_version=4, note='Synthetic withdrawal after answer')
                else:
                    with db.connect() as c:
                        if late_fault == 'budget':
                            changed = c.execute('UPDATE plan_item SET expected_minutes=expected_minutes+1 WHERE plan_item_id=?',
                                                (goals[0]['plan_item_id'],))
                        else:
                            changed = c.execute("UPDATE plan_item_event SET event_type='paused' WHERE plan_item_id=? AND event_type='started'",
                                                (goals[0]['plan_item_id'],))
                        assert changed.rowcount == 1
                        c.commit()
                return original_finalize(runtime, current_session, evidence_id)
            monkeypatch.setattr(AgentRuntimeStore, 'finalize_diagnostic_evaluation', change_after_saved)
        submitted = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json=payload)
        assert submitted.status_code == 200,submitted.text
        attempt = submitted.json()['attempt']
        assert attempt['submitted_answer'] == quote and attempt['correctness'] is None
        assert attempt['score_status'] == 'needs_review'
        follow = submitted.json()['local_learning_evaluation']
        if after_saved:
            after_saved(dict(db=db,client=client,settings=settings,gateway=gateway,child=CHILD,sid=sid,
                gateway_transport=transport, finish_client=lifecycle.close, client_options={'raise_server_exceptions':False}, gateway_factory=lambda: ModelGateway(settings,transport=httpx.MockTransport(transport)),
                prompt=prompt,attempt=attempt,follow=follow,quote=quote,voice=voice,posts=posts,
                evaluations=evaluations,command_state=command_state,headers=headers,source=source,payload=payload))
            return
        if late_fault:
            assert follow['status'] == 'blocked', follow
            assert not evaluations
            with db.connect() as c:
                rows = c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchall()
                assert [row[0] for row in rows] == [quote]
            return
        assert follow['status'] == 'feedback_required',follow
        evaluated = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert evaluated.status_code == 200,evaluated.text
        assert evaluated.json()['execution']['result']['formative_only'] is True
        assert len(evaluations) == 1 and evaluations[0]['response'] == quote
        assert evaluations[0]['question'] == source['prompt']
        serialized = json.dumps(posts,ensure_ascii=False)
        assert all(secret not in serialized for secret in (CHILD,sid,voice,prompt['prompt_id'],'voice_capture_binding','wav_base64','local_audit'))
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM mastery_state WHERE child_id=? AND knowledge_node_id=? AND evidence_count>0",
                (CHILD,source['knowledge_node_id'])).fetchone()
        if authored:
            from home_study_agent.agent_runtime import AgentRuntimeStore
            with application.state.repository.database.connect() as c:
                c.execute('BEGIN')
                changes = c.total_changes
                for kind in ('retry','independent_check'):
                    packet = AgentRuntimeStore.resolve_oral_followup(c,child_id=CHILD,session_id=sid,
                        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],kind=kind)
                    assert packet['status']=='ready', packet
                    assert not packet['action']['mastery_claim_allowed']
                assert c.total_changes==changes
        if raw_mode:
            from pathlib import Path
            from home_study_agent.school_spoken_score import original_response
            from home_study_agent.oral_teaching import project_oral_teaching
            with application.state.repository.database.connect() as c:
                saved_voice=c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone()
                if raw_mode=='never_store':
                    assert saved_voice and saved_voice[0] is None
                else:
                    # Simulated raw-retention boundary on generated test audio,
                    # not a claim that the product's cleanup scheduler ran.
                    assert saved_voice and saved_voice[0]
                    path=Path(saved_voice[0])
                    assert path.is_relative_to(tmp_path) and path.is_file()
                    path.unlink()
                    if raw_mode!='file_missing':
                        c.execute('UPDATE voice_input SET local_path=NULL WHERE voice_input_id=?',(voice,))
                    if raw_mode=='transcript_changed':
                        c.execute("UPDATE voice_input SET transcript='not the submitted answer' WHERE voice_input_id=?",(voice,))
                    elif raw_mode=='capture_cancelled':
                        c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=?",(voice,))
                    elif raw_mode=='binding_removed':
                        c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?',(voice,))
                    c.commit()
                c.execute('BEGIN')
                saved_prompt=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
                saved_attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
                with pytest.raises(ValueError):
                    original_response(c,saved_prompt,saved_attempt)
                teaching=project_oral_teaching(c,CHILD,sid,prompt['prompt_id'],prompt['item_version'])
                if raw_mode in {'transcript_changed','capture_cancelled','binding_removed'}:
                    assert teaching['status']=='unavailable' and teaching['response_text'] is None,teaching
                    assert not teaching['skill_scoring_allowed'] and not teaching['mastery_claim_allowed']
                    return
                assert teaching['status']=='content_self_check',teaching
                assert teaching['response_text']==quote and teaching['feedback']==[]
                assert all(point['status']=='not_assessed' for point in teaching['task_points'])
                assert not teaching['skill_scoring_allowed'] and not teaching['mastery_claim_allowed']
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        count = len(posts)
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json=payload)
        assert replay.status_code == 200,replay.text
        assert replay.json()['attempt']['attempt_id'] == attempt['attempt_id']
        assert len(posts) == count
        if help_request:
            command_state.update(control='learning.help_current',quote=help_request)
            help_voice = capture(client,settings,monkeypatch,sid,help_request,prompt,'help-audio',child=CHILD)
            helped = interpret(client,sid,help_request,'help-command',help_voice)
            if help_fault=='not_requested':
                assert helped['action']['type'] in {'none','stop_speech'},helped
                assert not tutor_calls
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==1
                return
            if helped['action']['type']!='ask_tutor':
                with db.connect() as c:
                    focus=[tuple(r) for r in c.execute('''SELECT pi.status,o.status FROM plan_item pi
                        JOIN daily_plan dp USING(plan_id) JOIN agent_objective o ON o.session_id=dp.session_id
                        WHERE pi.plan_item_id=?''',(goals[0]['plan_item_id'],))]
                pytest.fail(str((helped['response_text'],focus)))
            action = helped['action']
            assert action['question']==source['prompt']
            assert action['diagnostic_prompt_id']==prompt['prompt_id']
            assert action['diagnostic_item_version']==prompt['item_version']
            if help_fault=='source_before':
                with db.connect() as c:
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
                    c.commit()
            tutor_mode = True
            help_body={
                'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],
                'prompt_id':action['diagnostic_prompt_id'],'item_version':action['diagnostic_item_version'],
                'client_action_id':action['tutor_request_action_id']}
            if help_fault=='request_mismatch':
                help_body.update(question='请解释为什么一加一等于二',prompt_id=None,item_version=None)
            explained = client.post(f'/v1/sessions/{sid}/tutor',json=help_body)
            if help_fault in {'source_before','budget_during','request_mismatch'}:
                assert explained.status_code==409,explained.text
                assert len(tutor_calls)==int(help_fault=='budget_during')
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM tutor_turn WHERE session_id=?',(sid,)).fetchone()[0]==0
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==1
                return
            assert explained.status_code==200,explained.text
            assert explained.json()['answer'] and tutor_calls
            assert explained.json()['output_envelope']['local_teaching_control']['question_target']['binding']['prompt_id']==prompt['prompt_id']
            repeat = client.post(f'/v1/sessions/{sid}/tutor',json={
                'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],
                'prompt_id':action['diagnostic_prompt_id'],'item_version':action['diagnostic_item_version'],
                'client_action_id':action['tutor_request_action_id']})
            assert repeat.status_code==200 and repeat.json()['turn_id']==explained.json()['turn_id']
            assert len(tutor_calls)==1
            if help_fault=='receipt_wrong':
                with db.connect() as c:
                    c.execute("UPDATE tutor_turn SET client_action_id='another-command' WHERE turn_id=?",(explained.json()['turn_id'],))
                    c.commit()
            completed=client.put(f"/v1/agent-turns/{helped['agent_turn_id']}/outcome",json={
                'tool_status':'completed','final_response':explained.json()['answer'],
                'result':{'execution_receipt':{'capability':'learning.explain','action_type':'ask_tutor',
                    'execution_location':'local_model','status':'completed',
                    'completed_at':datetime.now(timezone.utc).isoformat(),
                    'output':{'tutor_turn_id':explained.json()['turn_id']}}}})
            if help_fault=='receipt_wrong':
                assert completed.status_code==409,completed.text
                assert '讲解' in completed.json()['message'] and '评分' not in completed.json()['message']
                with db.connect() as c:
                    assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',(helped['agent_turn_id'],)).fetchone()[0]=='pending'
                return
            assert completed.status_code==200,completed.text
            if help_fault=='long_explanation':
                assert 0<len(explained.json()['answer'])<4000
                assert explained.json()['answer'] == '先只看实数根存在需要满足的条件：你会怎样判断判别式的正负？'
                assert explained.json()['answer'].count('？') == 1
            if help_fault in {'history_root','malformed_focus'}:
                from home_study_agent.learning_question_help import teaching_receipt,snapshot
                with db.connect() as c:
                    if help_fault=='history_root':
                        c.execute("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.scheduled_writing_goal') WHERE agent_turn_id=?",
                            (requested.json()['agent_turn_id'],))
                    else:
                        c.execute("UPDATE diagnostic_prompt SET item_json='{}' WHERE prompt_id=?",(prompt['prompt_id'],))
                    c.commit()
                    c.execute('BEGIN')
                    if help_fault=='history_root':
                        with pytest.raises(ValueError):
                            teaching_receipt(c,helped['agent_turn_id'],historical=True)
                    else:
                        assert snapshot(c,sid,CHILD) is None
                return
            tutor_mode = False
            command_state.clear()
        stages = ('retry','independent_check') if continuation=='chain' else (continuation,) if continuation else ()
        for stage,kind in enumerate(stages):
            from home_study_agent.agent_runtime import AgentRuntimeStore
            with client.app.state.repository.database.connect() as c:
                c.execute('BEGIN')
                packet = AgentRuntimeStore.resolve_oral_followup(c,child_id=CHILD,session_id=sid,
                    prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],kind=kind)
                assert packet['status']=='ready', packet
                assert not packet['action']['mastery_claim_allowed']
            command_state.update(control='oral.'+kind,quote='我想再说一次' if kind=='retry' else '换一道没做过的题，我自己试试')
            voice2 = capture(client,settings,monkeypatch,sid,command_state['quote'],prompt,f'continue-audio-{stage}',child=CHILD)
            command = interpret(client,sid,command_state['quote'],f'continue-command-{stage}',voice2)
            assert command.get('runtime_capability')=='oral.'+kind, command
            path = f"/v1/agent-turns/{command['agent_turn_id']}/execute"
            if followup_fault in {'budget','source','capture'}:
                with db.connect() as c:
                    if followup_fault=='budget':
                        c.execute('UPDATE plan_item SET expected_minutes=expected_minutes+1 WHERE plan_item_id=?',
                            (goals[0]['plan_item_id'],))
                    elif followup_fault=='source':
                        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",
                            (packet['action']['diagnostic_item_id'],))
                    else:
                        c.execute("UPDATE voice_input SET transcript='changed source' WHERE voice_input_id=?",(voice,))
                    c.commit()
                rejected = client.post(path)
                assert rejected.status_code==409,rejected.text
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==1
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1
                return
            continued = client.post(path)
            assert continued.status_code==200,continued.text
            value = continued.json()['execution']['result']
            new_prompt = value['prompt']
            assert new_prompt, value
            assert new_prompt['prompt_id'] != prompt['prompt_id']
            assert (new_prompt['diagnostic_item_id']==prompt['diagnostic_item_id']) == (kind=='retry')
            assert value['required_hint_level']==(1 if kind=='retry' else 0)
            assert client.post(path).json()['execution']['result']['prompt']['prompt_id']==new_prompt['prompt_id']
            command_state.clear()
            quote = ('判别式是四减四k，大于等于零才有实数根，所以k小于等于一。'
                if kind=='retry' else '六的平方减去二十得到十六，大于零，所以有两个不相等的实数根。')
            answer_voice = capture(client,settings,monkeypatch,sid,quote,new_prompt,f'followup-answer-audio-{stage}',child=CHILD)
            answer_turn = interpret(client,sid,quote,f'followup-answer-turn-{stage}',answer_voice)
            assert answer_turn['action']['type']=='submit_diagnostic_answer',answer_turn
            submitted2 = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
                'prompt_id':new_prompt['prompt_id'],'item_version':new_prompt['item_version'],
                'answer':quote,'client_action_id':f'followup-answer-{stage}','authorized_turn_id':answer_turn['agent_turn_id']})
            assert submitted2.status_code==200,submitted2.text
            follow2 = submitted2.json()['local_learning_evaluation']
            with db.connect() as c:
                objective_id = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                    (command['agent_turn_id'],)).fetchone()[0]
            if follow2['status']=='blocked':
                from home_study_agent.formative_evaluation_projection import build_formative_projection
                with client.app.state.repository.database.connect() as c:
                    c.execute('BEGIN')
                    debug = build_formative_projection(c,child_id=CHILD,session_id=sid,prompt_id=new_prompt['prompt_id'],
                        item_version=new_prompt['item_version'],objective_id=objective_id)
                    receipts = [tuple(r) for r in c.execute('''SELECT p.status,s.status,t.tool_status FROM agent_plan p
                        JOIN agent_step s USING(plan_id) JOIN agent_turn t USING(agent_turn_id) WHERE p.agent_turn_id=?''',
                        (command['agent_turn_id'],))]
                pytest.fail(str((follow2,debug['reason'],receipts)))
            assert follow2['status']=='feedback_required',follow2
            if help_fault=='history_changed':
                from home_study_agent.formative_evaluation_projection import build_formative_projection
                with db.connect() as c:
                    c.execute("UPDATE tutor_turn SET answer='changed explanation' WHERE turn_id=?",(explained.json()['turn_id'],))
                    c.commit()
                    c.execute('BEGIN')
                    blocked=build_formative_projection(c,child_id=CHILD,session_id=sid,
                        prompt_id=new_prompt['prompt_id'],item_version=new_prompt['item_version'],objective_id=objective_id)
                    assert blocked['status']=='local_required' and blocked['payload'] is None,blocked
                return
            evaluation2 = client.post(f"/v1/agent-turns/{follow2['feedback_turn_id']}/execute")
            assert evaluation2.status_code==200,evaluation2.text
            assert evaluation2.json()['execution']['result']['formative_only'] is True
            assert len(evaluations)==stage+2 and evaluations[-1]['response']==quote
            assert evaluations[-1]['question']==new_prompt['item']['prompt']
            cloud = json.dumps(posts,ensure_ascii=False)
            assert all(private not in cloud for private in (CHILD,sid,voice2,answer_voice,
                new_prompt['prompt_id'],'oral_followup_binding','school_original','local_audit'))
            with client.app.state.repository.database.connect() as c:
                evidence2 = submitted2.json()['attempt']['evidence_id']
                assert c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?',(evidence2,)).fetchone()[0]==(1 if kind=='retry' else 0)
                assert not c.execute('SELECT 1 FROM mastery_state WHERE child_id=? AND knowledge_node_id=? AND evidence_count>0',
                    (CHILD,source['knowledge_node_id'])).fetchone()
            if followup_fault:
                from home_study_agent.formative_evaluation_projection import build_formative_projection
                with client.app.state.repository.database.connect() as c:
                    if followup_fault=='history_packet':
                        c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.oral_followup.action.parent_prompt_id','not-the-parent') WHERE prompt_id=?",
                            (new_prompt['prompt_id'],))
                    elif followup_fault=='history_receipt':
                        c.execute("UPDATE agent_step SET status='failed' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                            (command['agent_turn_id'],))
                    else:
                        assert followup_fault=='assistance' and kind=='retry'
                        c.execute('UPDATE learning_evidence SET hint_level=0 WHERE evidence_id=?',(evidence2,))
                    c.commit()
                    c.execute('BEGIN')
                    checked = build_formative_projection(c,child_id=CHILD,session_id=sid,prompt_id=new_prompt['prompt_id'],
                        item_version=new_prompt['item_version'],objective_id=objective_id)
                    assert checked['status']=='local_required' and checked['payload'] is None,checked
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==2
                return
            prompt = new_prompt
    if continuation:
        with TestClient(create_app(settings,model_gateway=gateway)) as client:
            count = len(posts)
            current = client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert current.status_code==200,current.text
            assert current.json()['status']=='answered'
            assert current.json()['attempt']['submitted_answer']==quote
            assert len(posts)==count
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==len(stages)+1


@pytest.mark.parametrize('continuation',['retry','independent_check','chain'])
def test_authored_school_oral_followup_after_answer_restart(tmp_path,monkeypatch,continuation):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,'authored:数学',None,True,None,continuation=continuation)


@pytest.mark.parametrize('fault',['budget','source','capture','history_packet','history_receipt','assistance'])
def test_authored_school_oral_followup_rejects_changed_authority(tmp_path,monkeypatch,fault):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,'authored:数学',None,True,None,continuation='retry',followup_fault=fault)


def test_answered_school_question_natural_help_then_retry_and_new_question(tmp_path,monkeypatch):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(tmp_path,monkeypatch,'authored:数学',None,True,None,
        continuation='chain',help_request='我刚才其实是猜的，为什么要这样判断，你能带我想明白吗？')


@pytest.mark.parametrize('mode',['never_store','cleared','file_missing','transcript_changed','capture_cancelled','binding_removed'])
def test_school_transcript_teaching_survives_raw_retention_not_source_loss(tmp_path,monkeypatch,mode):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,'authored:数学',None,True,None,
        continuation='chain',help_request='我刚才其实是猜的，为什么要这样判断，你能带我想明白吗？',raw_mode=mode)


@pytest.mark.parametrize('subject', ['authored:数学','authored:语文','authored:英语','authored:物理',
    'authored:化学','authored:历史','authored:道德与法治',
    'authored-p4:数学','authored-p4:语文','authored-p4:英语'])
def test_school_subjects_reference_feedback_without_raw_recording(tmp_path,monkeypatch,subject):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,subject,None,subject=='authored:数学',None,raw_mode='never_store')


@pytest.mark.parametrize('fault',['not_requested','source_before','budget_during','receipt_wrong','history_changed',
    'request_mismatch','history_root','malformed_focus','long_explanation'])
def test_school_question_help_rejects_changed_authority(tmp_path,monkeypatch,fault):
    test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,'authored:数学',None,True,None,
        continuation='chain',help_request='先别讲，让我自己想一想' if fault=='not_requested'
            else '我刚才其实是猜的，为什么要这样判断，你能带我想明白吗？',help_fault=fault)

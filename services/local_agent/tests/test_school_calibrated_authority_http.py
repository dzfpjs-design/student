"""Signed SYNTHETIC calibration corpus -> actual score HTTP/persist.

Published originals and signatures are real local service operations. Historical
model execution rows below are declared synthetic transport receipts, not model
quality evidence. The new non-holdout response uses the actual ModelGateway and
executor. No authority resolver, score receipt or policy threshold is mocked.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.model_gateway import ModelGateway, ModelEndpoint, _scorer_request_provenance
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import digest, source_snapshot
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.writing_rubric import writing_dimensions
from test_calibration_evaluator_host import signer
from test_school_open_reviews import school_case, CHILD
from test_writing_question_authorization import LICENSE

ROOT = f'/v1/parent/children/{CHILD}/rubric-calibration'


def voice_answer(case, monkeypatch, prompt, text, key):
    from test_voice_runtime_binding import capture, interpret
    db, client, headers, sid = case[:4]
    monkeypatch.setattr('test_voice_runtime_binding.CHILD', CHILD)
    assert client.put('/v1/parent/privacy',headers=headers,json={
        'raw_media_storage_allowed':True,'cloud_audio_allowed':False}).status_code == 200
    text = '我的答案是' + text
    voice = capture(client,case[-1],monkeypatch,sid,text,prompt,key+'-voice',child=CHILD)
    turn = interpret(client,sid,text,key+'-turn',voice)
    assert turn['action']['type'] == 'submit_diagnostic_answer', turn
    answer = DiagnosticPromptService(db).answer(sid,prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],answer=text,authorized_turn_id=turn['agent_turn_id'],
        client_action_id=key+'-answer')
    return answer, text


def published_corpus(case, monkeypatch, *, spoken=False):
    db, client, headers, sid, baseline, near, far, supply, settings = case
    with db.connect() as c:
        scope = json.loads(c.execute("SELECT scope_json FROM curriculum_supply_selection WHERE child_id=? AND subject='数学' ORDER BY version DESC LIMIT 1", (CHILD,)).fetchone()[0])
        excerpts = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (scope['textbook_source_id'],)).fetchall()[3:]
        payload = json.loads(c.execute('SELECT payload_json FROM curriculum_supply_package WHERE candidate_id=?', (baseline['candidate_id'],)).fetchone()[0])
        spec = {k:payload[k] for k in ('seed_item_id','stage','excerpt_id','requirement_id','standard_page','standard_quote','validator','explanation','difficulty')}
    prompts, originals = DiagnosticPromptService(db), WritingOriginalService(db)
    answers = []
    for i, (eid,) in enumerate(excerpts):
        draft = supply.generate_draft(CHILD, {**spec, 'excerpt_id':eid, 'stage':'parallel'}, author='synthetic-author', action_id=f'holdout-{i}-draft')
        cid = draft['candidate_id']
        supply.validate(CHILD, cid, actor='local-validator', action_id=f'holdout-{i}-validate', expected_version=1)
        supply.review(CHILD, cid, actor='parent', decision='approve', note='Synthetic authored source only', action_id=f'holdout-{i}-review', expected_version=2)
        item = supply.publish(CHILD, cid, actor='parent', action_id=f'holdout-{i}-publish', expected_version=3)
        p = prompts.prepare(sid, track='school', preferred_item_id=item['diagnostic_item_id'], client_action_id=f'holdout-{i}-prompt')['prompt']
        text = f'我先列出条件，再计算判别式，检查符号与实数根个数。合成原稿编号{i}。'
        if spoken:
            answer, text = voice_answer(case,monkeypatch,p,text,f'holdout-{i}')
        else:
            original = originals.capture_native_response(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], text=text, client_action_id=f'holdout-{i}-original')
            answer = prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text,
                writing_original_artifact_id=original['artifact_id'], client_action_id=f'holdout-{i}-answer')
        answers.append((p, answer['attempt'], text))
    assert len(answers) == 200
    return answers


def historical_score(db, sid, protocol, scorer, provenance, entry, score):
    """Construct disclosed historical transport records before immutable freeze."""
    p, attempt, text = entry
    now = datetime.now(timezone.utc)
    objective, turn, plan, step, call = [uuid4().hex for _ in range(5)]
    rubric = RubricEvidenceService(db)
    with db.connect() as c:
        source = source_snapshot(c, CHILD, attempt['evidence_id'])
        binding = {'version':'rubric_calibration_execution_v1', 'status':'bound', 'mode':'holdout',
            'protocol_id':protocol['protocol_id'], 'stratum':'ordinary', 'child_id':CHILD, 'session_id':sid,
            'source':source, 'assessment_binding': {'protocol_id':protocol['protocol_id'],
                'protocol_hash':protocol['protocol_hash'], 'scorer_hash':digest(scorer), 'source_version':source['version']}}
        criteria = ['说明题中条件和所用关系', '用明确的过程支持结论，并说明其适用边界']
        c.execute("INSERT INTO agent_objective VALUES (?,?,?,'learning','Synthetic holdout',?,'diagnose','completed',?,?,?,NULL)",
            (objective,CHILD,sid,json.dumps(criteria),p['created_at'],now.isoformat(),now.isoformat()))
        c.execute("INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,'learning.evaluate_response','','{}','synthetic-historical',?,?)", (turn,CHILD,sid,text,now.isoformat(),turn))
        c.execute("INSERT INTO agent_plan VALUES (?,?,?,?,?,'synthetic-historical','{}','{}','completed',?,?)", (plan,objective,CHILD,sid,turn,now.isoformat(),now.isoformat()))
        args = dict(response=text, question_asked=p['item']['prompt'], subject='数学', evidence_id=attempt['evidence_id'])
        c.execute("INSERT INTO agent_step VALUES (?,?,0,'learning.evaluate_response','1','local_model','medium',?,'[]','completed',?,NULL,NULL,?,?)", (step,plan,json.dumps(args),step,now.isoformat(),now.isoformat()))
        c.execute("""INSERT INTO model_call(call_id,request_id,session_id,child_id,purpose,provider,model,status,input_chars,prompt_version,context_manifest_json,risk_level,provider_preference,policy_version,verification_status,safety_status,created_at)
            VALUES (?,?,?,?,'learning_outcome_evaluation',?,?,'completed',0,'synthetic-historical','{}','medium','isolated','synthetic','partially_verified','passed',?)""", (call,call,sid,CHILD,scorer['provider'],scorer['model'],now.isoformat()))
        record = dict(child_id=CHILD, session_id=sid, objective_id=objective, step_id=step, subject='数学',
            question_asked=p['item']['prompt'], child_response=text, source_evidence_id=attempt['evidence_id'],
            success_criteria_json=json.dumps(criteria), scorer_provenance=provenance, calibration=binding)
        if source['modality'] == 'original_spoken_text':
            from home_study_agent.open_response_profile import profile_for_item
            item = DiagnosticPromptService._item(c,CHILD,p['diagnostic_item_id'])
            node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone())
            definitions = profile_for_item(item,node)['dimensions']
        else:
            definitions = writing_dimensions(c,CHILD,p['diagnostic_item_id'])
        dims = [{'dimension_id':d['dimension_id'], 'score':score, 'response_quotes':[text], 'rationale':'Synthetic holdout score'}
                for d in definitions]
        c.execute("INSERT INTO learning_evaluation_execution VALUES (?,'completed','synthetic-historical',?,?,?,?,NULL,NULL)",
            (step,now.isoformat(),json.dumps(record),call,json.dumps({'rubric_dimensions':dims})))
        current = rubric.freeze(c,record,{'rubric_dimensions':dims,'independence_eligible':False},call,now)
        c.commit()
    return current


def qualify(client, headers, db, sid, answers, spec, sign, now):
    scorer = {**spec['scorer'], 'model_revision':'synthetic-release-v1', 'revision_receipt_id':'release'}
    scope = dict(subject=spec['source']['subject'], grade=spec['source']['grade'], task_family=scorer['profile_id'], strata=['ordinary'])
    revision = dict(child_id=CHILD, receipt_id='release', status='current', provider=scorer['provider'], model=scorer['model'],
        model_revision=scorer['model_revision'], verification_method='signed_release_manifest', evidence_hash='a'*64,
        endpoint_scope_sha256=spec['endpoint_scope_sha256'], verified_at=now.isoformat(), expires_at=(now+timedelta(days=1)).isoformat())
    holdout = dict(child_id=CHILD, receipt_id='holdout', status='current', corpus_kind='observed_original',
        blind_protocol_version='blind_labels_v1', provenance_receipt_hash='b'*64, scope_hash=digest(scope), frozen_at=now.isoformat(),
        cases=[dict(evidence_id=a['evidence_id'], group_hash=digest(['synthetic-distinct-source',p['diagnostic_item_id']]), stratum='ordinary',source_ids=[]) for p,a,t in answers],
        application_scope=[dict(profile_id=scorer['profile_id'],answer_types=['open_response'],stratum='ordinary')])
    for kind, payload in [('scorer_revision',revision),('holdout',holdout)]:
        r = client.post(ROOT+'/authority/import', headers=headers, json=sign(kind,payload))
        assert r.status_code == 200, r.text
    r = client.post(ROOT+'/protocols/freeze', headers=headers, json=dict(scorer=scorer,scope=scope,holdout_receipt_id='holdout',client_action_id='freeze-synthetic'))
    assert r.status_code == 200, r.text
    protocol = r.json()['current']
    protocol_started = r.json()['receipt']['created_at']
    provenance = dict(version='model_scorer_provenance_v1', provider=scorer['provider'],requested_model=scorer['model'],reported_model=scorer['model'],provider_system_fingerprint=None,
        endpoint_scope_sha256=spec['endpoint_scope_sha256'],system_prompt_sha256=scorer['prompt_hash'],parameters_sha256=scorer['parameters_hash'],immutable_revision_verified=False,local_only=True,cloud_transmission_allowed=False)
    rubric = RubricEvidenceService(db)
    for i, entry in enumerate(answers):
        score = i//50
        current = historical_score(db,sid,protocol,scorer,provenance,entry,score)
        aid = current['assessment_id']
        labels = []
        for j in range(2):
            label = rubric.review(CHILD,aid,expected_revision=j,client_action_id=f'label-{i}-{j}',reviewer_id=f'synthetic-reviewer-{j}',reviewer_role='teacher',operation='approve',reason='Synthetic blind label, not real child/model quality',independent=False,hint_level=1,
                dimensions=[dict(dimension_id=d['dimension_id'],score=score,response_quotes=[current['response']],rationale='Synthetic anchor label') for d in current['dimensions']],
                criterion_evidence=[dict(criterion=v,response_quote=current['response']) for v in current['criteria']])
            with db.connect() as c:
                event = dict(c.execute('SELECT * FROM rubric_review_event WHERE event_id=?',(label['event_id'],)).fetchone())
                fingerprint = c.execute('SELECT fingerprint FROM rubric_assessment WHERE assessment_id=?',(aid,)).fetchone()[0]
                source = source_snapshot(c,CHILD,entry[1]['evidence_id'])
            rid = f'blind-{i}-{j}'
            proof = dict(receipt_id=rid,child_id=CHILD,status='current',identity_authority='independent_reviewer_registry_v1',identity_receipt_hash=digest(['synthetic-reviewer',j]),display_receipt_hash=digest(['synthetic-display',rid]),submission_receipt_hash=digest(['synthetic-submit',rid]),mask_version='blind_no_predictions_or_peer_labels_v1',protocol_hash=protocol['protocol_hash'],assessment_fingerprint=fingerprint,evidence_id=entry[1]['evidence_id'],source_version=source['version'],reviewer_principal_id=f'synthetic-principal-{j}',reviewer_id=event['reviewer_id'],review_event_id=event['event_id'],review_event_hash=digest(event),displayed_at=protocol_started,submitted_at=event['created_at'])
            r = client.post(ROOT+'/authority/import', headers=headers, json=sign('blind_label',proof))
            assert r.status_code == 200, r.text
            labels.append(rid)
        r = client.post(ROOT+'/'+protocol['protocol_id']+'/cases',headers=headers,json=dict(assessment_id=aid,blind_receipt_ids=labels,expected_revision=i+1,client_action_id=f'record-{i}'))
        assert r.status_code == 200, r.text
        if i%50 == 49:
            print('synthetic signed calibration cases:',i+1,flush=True)
    r = client.post(ROOT+'/'+protocol['protocol_id']+'/evaluate',headers=headers,json=dict(expected_revision=201,client_action_id='qualify'))
    assert r.status_code == 200 and r.json()['current']['score_qualified'], r.text
    return protocol['protocol_id']


def test_calibration_voice_fixture_keeps_actual_child_owner(tmp_path,monkeypatch):
    fixture=school_case.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param='spoken_public'))
    case=next(fixture)
    try:
        db,_,_,sid,baseline=case[:5]
        prompt=DiagnosticPromptService(db).prepare(sid,track='school',
            preferred_item_id=baseline['diagnostic_item_id'],client_action_id='owner-check')['prompt']
        answer,text=voice_answer(case,monkeypatch,prompt,'我会先看判别式的符号，再说明实数根的个数。','owner-check')
        assert answer['attempt']['submitted_answer']==text
        with db.connect() as c:
            assert c.execute('SELECT child_id FROM diagnostic_attempt').fetchone()[0]==CHILD
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
    finally:
        fixture.close()


@pytest.mark.parametrize('mode', ['public','spoken_public'])
def test_signed_school_certificate_actual_execute_and_withdrawal(tmp_path,monkeypatch,mode):
    if os.environ.get('HSC_SYNTHETIC_CALIBRATION_CHECKPOINT_DIR'):
        assert not any(part.startswith('pytest-of-') for part in tmp_path.parts), (
            'Persistent checkpoints require an explicit --basetemp outside pytest retention; '
            'otherwise subsequent tests delete the original recordings.')
    # Extend only this synthetic source before its first immutable publication.
    import_source = KnowledgeBaseService.import_source
    def with_corpus(self,child,values):
        if values['title'] == 'Synthetic school transfer questions':
            lines = values['content_text'].splitlines()
            original = json.loads(lines[0])
            lines.extend(json.dumps({**original,'prompt':f'不直接求根，分析 x²+{i+10}x+{i+20}=0 的实数解情况，并说明推理与条件。'},ensure_ascii=False) for i in range(200))
            values = {**values,'content_text':'\n'.join(lines)}
        return import_source(self,child,values)
    monkeypatch.setattr(KnowledgeBaseService,'import_source',with_corpus)
    spoken = mode == 'spoken_public'
    fixture = school_case.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param=mode))
    case = next(fixture)
    try:
        db, old_client, old_headers, sid, baseline = case[:5]
        answers = published_corpus(case,monkeypatch,spoken=spoken)
        prompts = DiagnosticPromptService(db)
        assert old_client.put('/v1/parent/privacy',headers=old_headers,json={'cloud_model_text_allowed':True}).status_code == 200
        permission = f'/v1/parent/children/{CHILD}/writing-question-authorizations/'+baseline['diagnostic_item_id']
        view = old_client.get(permission,headers=old_headers).json()
        authorized = old_client.post(permission+'/grant',headers=old_headers,json={
            **{k:view[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':0,'client_action_id':'live-purpose','license_basis':LICENSE})
        assert authorized.status_code == 200, authorized.text
        p = prompts.prepare(sid,track='school',preferred_item_id=baseline['diagnostic_item_id'],client_action_id='live-original')['prompt']
        # Structured original goal fixture, frozen BEFORE submission, not a
        # claim of voice planning. The score executor below is actual HTTP.
        import test_formative_evaluation_projection as original_goal
        monkeypatch.setattr(original_goal,'CHILD',CHILD)
        original_goal.objective(db,sid,p,None)
        text = '我根据二次项系数和判别式的符号说明实数根的个数，再核对定义域及适用条件。'
        if spoken:
            answer, text = voice_answer(case,monkeypatch,p,text,'live-spoken')
        else:
            original = WritingOriginalService(db).capture_native_response(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],text=text,client_action_id='live-written')
            answer = prompts.answer(sid,prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,writing_original_artifact_id=original['artifact_id'],client_action_id='live-answer')
        settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,model_provider='qwen',model_name='isolated-model',model_api_key='not-a-key',model_base_url='https://isolated.invalid/v1',data_encryption_key=case[-1].data_encryption_key)
        posts = []
        async def transport(request):
            body = json.loads(request.content)
            posts.append(body)
            ctx = json.loads(body['messages'][1]['content'])
            rubric = ctx.get('rubric',{})
            dims = rubric.get('dimensions',ctx.get('rubric_dimensions',[]))
            response = ctx.get('response',ctx.get('child_response'))
            result = dict(understanding='independent',confidence=.92,evidence_summary='Synthetic transport only',spoken_feedback='请再核对所用条件。',next_check='能说明判别式符号吗？',criterion_evidence=[],rubric_dimensions=[dict(dimension_id=d['dimension_id'],score=3,response_quotes=[response],rationale='Synthetic score, not model validation') for d in dims])
            from home_study_agent.learning_outcome_evaluator import FORMATIVE_SYSTEM_PROMPT
            if body['messages'][0]['content']==FORMATIVE_SYSTEM_PROMPT:
                from formative_response_fixture import criterion_reply
                result=criterion_reply(ctx,result)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
        fixture.close()
        app = create_app(settings,model_gateway=ModelGateway(settings,transport=httpx.MockTransport(transport)))
        with TestClient(app,client=('127.0.0.1',41000),raise_server_exceptions=False) as client:
            headers = {'X-HSC-Parent':client.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']}
            follow = app.state.agent_runtime.finalize_diagnostic_evaluation(sid,answer['attempt']['evidence_id'])
            assert follow.get('feedback_turn_id'), follow
            tid = follow['feedback_turn_id']
            spec = client.get(ROOT+'/evaluations/'+tid+'/request-spec',headers=headers)
            if spec.status_code != 200:
                from home_study_agent.rubric_calibration_host import describe_execution
                describe_execution(app.state.learning_outcome_evaluator, CHILD, tid)
            assert spec.status_code == 200,spec.text
            assert spec.json()['scorer']['modality'] == ('original_spoken_text' if spoken else 'original_written_text')
            sign, now = signer(tmp_path,monkeypatch,children=[CHILD])
            pid = qualify(client,headers,db,sid,answers,spec.json(),sign,now)
            if spoken:
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 0
            from calibration_execution_measurement import ExecutionMeasurement, checkpoint
            from pathlib import Path
            root = Path(os.environ.get('HSC_SYNTHETIC_CALIBRATION_CHECKPOINT_DIR', str(tmp_path/'signed-checkpoints')))
            metadata = dict(child_id=CHILD,session_id=sid,protocol_id=pid,turn_id=tid,
                source_database=str(db.path),synthetic_only=True,real_model_quality=False)
            checkpoint(db,root,'qualified-before-execute',os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE'],metadata)
            measurement = ExecutionMeasurement(monkeypatch,db,RubricCalibrationService,
                additional_databases=(app.state.repository.database,),
                additional_services=(app.state.rubric_calibration,))
            r, execute_metrics = measurement.run(lambda: client.post('/v1/agent-turns/'+tid+'/execute'), lock_probe=True)
            assert r.status_code == 200,r.text
            assert len(posts) == 1
            with db.connect() as c:
                from home_study_agent.writing_score import current_written_score
                aid = c.execute('SELECT assessment_id FROM rubric_assessment WHERE step_id IN (SELECT step_id FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=?)',(tid,)).fetchone()[0]
                if spoken:
                    from home_study_agent.school_spoken_score import current_score
                    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(p['prompt_id'],)).fetchone()
                    attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(answer['attempt']['attempt_id'],)).fetchone()
                    score = current_score(c,CHILD,aid,prompt,attempt,now=datetime.now(timezone.utc))
                else:
                    score = current_written_score(c,CHILD,aid,now=datetime.now(timezone.utc))
                assert score['decision']['status'] == 'calibrated_model'
                assert score['decision']['passed']
                task = dict(c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND status='pending'",(answer['attempt']['evidence_id'],)).fetchone())
                assert task['score_authority_id'] == score['decision']['authority_id']
                assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',(aid,)).fetchone()[0] == 0
            def qualified_receipt():
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    return RubricCalibrationService(db).qualified_score_receipt(c,CHILD,aid,
                        protocol_id=pid,as_of=datetime.now(timezone.utc))
            receipt, receipt_metrics = measurement.run(qualified_receipt,lock_probe=True)
            assert receipt['score_qualified']
            checkpoint(db,root,'executed-before-revoke',os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE'],
                {**metadata,'assessment_id':aid,'review_task_id':task['review_task_id']})
            (root/'online-metrics.json').write_text(json.dumps(dict(execute=execute_metrics,
                qualified_receipt=receipt_metrics,transport='immediate declared synthetic',qualified=True),indent=2))
            r = client.post(ROOT+'/'+pid+'/revoke',headers=headers,json={'expected_revision':202,'client_action_id':'certificate-revoke'})
            assert r.status_code == 200,r.text
            with db.connect() as c:
                from home_study_agent.school_open_reviews import reconcile_school_reviews
                c.execute('BEGIN IMMEDIATE')
                assert task['review_task_id'] in reconcile_school_reviews(c,CHILD)['cancelled']
                c.commit()
    finally:
        fixture.close()

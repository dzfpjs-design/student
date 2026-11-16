"""School paper originals through actual voice/runtime routes; model/OCR are synthetic."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.child_model import ChildModelService
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_outcome_coordinator import LearningOutcomeCoordinator
from test_diagnostic_prompt_http import start
from test_open_response_supply_http import CHILD, prepared, publish_open
from test_paper_writing_runtime_http import PaperGateway, synthetic_page
from test_voice_runtime_binding import capture, interpret
from test_writing_question_authorization import LICENSE
from test_writing_voice_runtime_http import readback_receipt
from test_writing_followup_paper_http import revised_page


def published_pair(tmp_path,subject,with_material=False):
    db,supply,spec,source,_ = prepared(tmp_path,subject,license_status='public')
    if with_material:
        from home_study_agent.open_response_materials import bind_material_excerpt
        material = KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',
            title='Isolated shared question conditions',subject=subject,grade_band='初三',
            version_label='isolated-material-1',source_ref='isolated:shared-conditions',license_status='public',
            parent_reviewed=True,content_text=('仅讨论实数范围内的方程解，二次项系数不为零。' if subject=='数学' else
                '实验温度保持相同，电压相同，材料保持相同；实验读数尚未提供，不能把实验方案当作已经测得的结论。')))
        with db.connect() as c:
            excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(material['source_id'],)).fetchone()[0]
            binding = bind_material_excerpt(c, child_id=CHILD, excerpt_id=excerpt, material_key='conditions')
        source.update(schema='reviewed_open_response_source_v2',context_requirement='external_material',
                      material_semantics='text_only',material_bindings=[binding])
    second={**source,'prompt':('分析 x²-4x+4=0 在实数范围内的解的情况，并写出判别依据和推理过程。' if subject=='数学' else
        '同种材料制成的两段导体长度相同，横截面积不同。说明检验横截面积影响电阻的实验，写出控制条件和结论边界。')}
    imported=KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',title='Two isolated authored tasks',
        subject=subject,grade_band='初三',version_label='isolated-open-2026',source_ref='isolated-authored-pair',
        license_status='public',parent_reviewed=True,content_text='\n'.join(json.dumps(row,ensure_ascii=False) for row in (source,second))))
    with db.connect() as c:
        scope=json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection WHERE child_id=?',(CHILD,)).fetchone()[0])
        excerpts=[row[0] for row in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence',(imported['source_id'],))]
    supply.select_curriculum(CHILD,{**scope,'textbook_source_id':imported['source_id']},actor='parent',action_id='pair-scope',expected_version=1)
    first=publish_open(supply,{**spec,'excerpt_id':excerpts[0]})
    draft=supply.generate_draft(CHILD,{**spec,'excerpt_id':excerpts[1]},author='author',action_id='second-draft')
    cid=draft['candidate_id']
    supply.validate(CHILD,cid,actor='validator',action_id='second-validate',expected_version=1)
    supply.review(CHILD,cid,decision='approve',actor='parent',note='Checked distinct original question and reference criteria',
        action_id='second-review',expected_version=2)
    other=supply.publish(CHILD,cid,actor='parent',action_id='second-publish',expected_version=3)
    assert other['eligible'] and first['eligible']
    return db,source,first,other


@pytest.mark.parametrize('subject',['数学','物理'])
@pytest.mark.parametrize('next_kind',['revision','independent_check'])
@pytest.mark.parametrize('with_material',[False,True],ids=['self-contained','material'])
def test_school_paper_voice_feedback_revision_restart_and_no_false_mastery(tmp_path,monkeypatch,subject,next_kind,with_material,transport_fault=None,formula_revision=False):
    db,source,published,other = published_pair(tmp_path,subject,with_material)
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    monkeypatch.setattr('test_voice_runtime_binding.CHILD',CHILD)
    def no_image_cloud(*args,**kwargs): raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_image_cloud)
    png,text = synthetic_page(monkeypatch)
    # Deliberately irrelevant text: exercising provenance and feedback routing,
    # not claiming a correct mathematical/physical solution or OCR accuracy.
    gateway = PaperGateway()
    path = f'/v1/parent/children/{CHILD}/writing-question-authorizations/'+published['diagnostic_item_id']
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True}).status_code==200
        initial = client.get(path,headers=headers).json()
        def material_rights(view):
            return ({'material_license_bases':[
                {**{key:row[key] for key in ('material_key','source_version','text_hash')},'license_basis':LICENSE}
                for row in view['preview']['materials']]} if with_material else {})
        granted = client.post(path+'/grant',headers=headers,json={
            **{k:initial[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':0,'client_action_id':'grant','license_basis':LICENSE,**material_rights(initial)})
        assert granted.status_code==200,granted.text
        other_path=f'/v1/parent/children/{CHILD}/writing-question-authorizations/'+other['diagnostic_item_id']
        other_initial=client.get(other_path,headers=headers).json()
        other_grant=client.post(other_path+'/grant',headers=headers,json={
            **{k:other_initial[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':0,'client_action_id':'second-question-grant','license_basis':LICENSE,**material_rights(other_initial)})
        assert other_grant.status_code==200,other_grant.text
        sid = start(client,CHILD)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
            c.commit()
        ready = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
            'track':'school','subject':subject,'preferred_item_id':published['diagnostic_item_id'],'client_action_id':'school-prepare'})
        assert ready.status_code==200,ready.text
        prompt = ready.json()['prompt']
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'school-monitor'}).status_code==200
        page = client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
            'image_base64':base64.b64encode(png).decode(),'client_action_id':'school-page'})
        assert page.status_code==200,page.text
        artifact=page.json()
        revision=DocumentMonitoringService(db).record_revision(sid,artifact['artifact_id'],'school-paper','revision')
        def say(quote,key,control):
            key='school-'+key
            gateway.quote,gateway.control=quote,control
            voice=capture(client,settings,monkeypatch,sid,quote,prompt,key+'-voice',child=CHILD)
            return interpret(client,sid,quote,key,voice)
        say('写完了，帮我看看过程','look','writing.read_original')
        reading=say('这页只有我写的答案','scope','writing.read_original')
        assert reading['action']['type']=='read_writing_original',reading
        playback=readback_receipt(client,sid,reading)
        assert client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
            'final_response':'原稿已读回','tool_status':'completed','result':playback}).status_code==200
        confirmation=say('对，是我写的，识别没有错','confirm','writing.confirm_original')
        submitted=client.post(f"/v1/agent-turns/{confirmation['agent_turn_id']}/execute")
        assert submitted.status_code==200,submitted.text
        follow=submitted.json()['turn']['local_evaluation']
        assert follow['status']=='feedback_required',follow
        if transport_fault:
            from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService
            def revoke_during_post():
                WritingQuestionAuthorizationService(db).revoke(CHILD,
                    diagnostic_item_id=published['diagnostic_item_id'],expected_revision=1,
                    client_action_id='material-in-flight-revoke',actor_id='parent_ui')
            gateway.mutate=revoke_during_post
        evaluated=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        if transport_fault:
            assert evaluated.status_code==409,evaluated.text
            assert len(gateway.posts)==1
            with db.connect() as c:
                assert not c.execute('SELECT 1 FROM rubric_assessment').fetchone()
                assert not c.execute("SELECT 1 FROM learning_evaluation_execution WHERE state IN ('completed','response_ready')").fetchone()
                assert c.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0]==artifact['extracted_text']
            retried=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
            assert retried.status_code==409,retried.text
            assert len(gateway.posts)==1
            return
        assert evaluated.status_code==200,evaluated.text
        assert evaluated.json()['execution']['result']['formative_only']
        assert len(gateway.evaluations)==1
        payload=json.loads(gateway.evaluations[0].messages[1]['content'])
        assert set(payload)=={'question','response','rubric'} and payload['response']==artifact['extracted_text']
        if with_material:
            assert prompt['item']['stimulus_text'] and prompt['item']['stimulus_text'] in payload['question']
            assert source['material_bindings'][0]['source_id'] not in json.dumps(payload)
            assert source['material_bindings'][0]['excerpt_id'] not in json.dumps(payload)
        if subject=='物理':
            assert {d['dimension_id'] for d in payload['rubric']['dimensions']}=={'claim','evidence','reasoning','limitations'}
        with db.connect() as c:
            record=c.execute('SELECT assessment_id,payload_json FROM rubric_assessment').fetchone()
            assessment_id=record[0]
            assessment=json.loads(record[1])
            assert assessment['profile'].startswith('school_open_')
            assert {d['dimension_id'] for d in assessment['dimensions']}=={d['dimension_id'] for d in payload['rubric']['dimensions']}
            assert c.execute("SELECT COUNT(*) FROM agent_outcome WHERE outcome_level IN ('independent','transferred','retained')").fetchone()[0]==0
        review_path=f'/v1/parent/children/{CHILD}/learning-rubrics/{assessment_id}'
        detail=client.get(review_path,headers=headers)
        assert detail.status_code==200,detail.text
        evidence=detail.json()['writing_review']
        assert evidence['required'] and evidence['profile']=='school_open_written_v1'
        human_review={'expected_revision':0,'client_action_id':'school-original-review','operation':'approve',
            'reason':'The actual original does not answer this school question; preserve corrective feedback.',
            'independent':False,'hint_level':1,'review_profile':evidence['profile'],
            'dimensions':[{'dimension_id':d['dimension_id'],'score':0,'response_quotes':[artifact['extracted_text']],
                'rationale':'This original response does not address the stated problem.'} for d in assessment['dimensions']],
            'criterion_evidence':[{'criterion':point,'response_quote':artifact['extracted_text']} for point in assessment['criteria']],
            'writing_evidence':{'binding_version':evidence['binding']['binding_version'],
                'task_point_evidence':[{'task_point':point,'response_quote':artifact['extracted_text']}
                    for point in evidence['binding']['task_points']]}}
        wrong_profile=client.post(review_path+'/review',headers=headers,json={**human_review,'review_profile':'pet_writing_text_v1'})
        assert wrong_profile.status_code==409,wrong_profile.text
        reviewed=client.post(review_path+'/review',headers=headers,json=human_review)
        assert reviewed.status_code==200,reviewed.text
        assert not reviewed.json()['qualification']['eligible_for_mastery_gate']
        controls=ChildModelService(db).tutor_controls(CHILD,1,None,{},session_id=sid,subject=subject,
            knowledge_node_id=source['knowledge_node_id'],task_type='diagnostic')
        assert controls['audit']['teaching_context']['learning']['status']=='recheck_needed'
        assert controls['audit']['controls']['explanation_pacing']=='short_step_then_restate'
        capability='writing.revise_original' if next_kind=='revision' else 'writing.independent_check'
        chosen=say('我想按照反馈自己改一遍' if next_kind=='revision' else '给我一道没做过的题，我自己试试','revise',capability)
        assert chosen.get('runtime_capability')==capability,chosen
        revised=client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert revised.status_code==200,revised.text
        assert 'prompt' in revised.json()['execution']['result'],revised.json()['execution']['result']
        next_prompt=revised.json()['execution']['result']['prompt']
        assert next_prompt['prompt_id']!=prompt['prompt_id']
        assert next_prompt['diagnostic_item_id']==(prompt['diagnostic_item_id'] if next_kind=='revision' else other['diagnostic_item_id'])
        prompt=next_prompt
        if subject=='数学':
            # This transport fixture supplies ordinary printed-text OCR, not a
            # verified formula recognizer. Formula refusal has its own case.
            second_answer=('The coefficients of the quadratic are one, two and five. The discriminant is negative sixteen. '
                'It is negative, so the quadratic has no real roots. This conclusion concerns real numbers, not complex roots.'
                if next_kind=='revision' else
                'The coefficients of the quadratic are one, negative four and four. The discriminant is zero. '
                'The quadratic has two equal real roots, both two. The discriminant rule applies because the leading coefficient is nonzero.')
        else:
            variable='length' if next_kind=='revision' else 'cross-sectional area'
            controlled='cross-sectional area' if next_kind=='revision' else 'length'
            second_answer=(f'Compare conductors of the same material and {controlled} at the same temperature, changing only {variable}. '
                'Apply the same voltage and measure current, then divide voltage by current to compare resistance. Repeat the measurements. '
                'No readings are supplied, so this is a testable design, not an observed result or a proof for every temperature.')
            if formula_revision:
                second_answer=second_answer.replace('divide voltage by current','use R=U/I')
        second_png,second_text=revised_page(monkeypatch,next_kind,text=second_answer)
        page2=client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
            'image_base64':base64.b64encode(second_png).decode(),'client_action_id':'school-second-page'})
        assert page2.status_code==200,page2.text
        assert page2.json()['artifact_id']!=artifact['artifact_id'] and page2.json()['extracted_text']==second_text
        DocumentMonitoringService(db).record_revision(sid,page2.json()['artifact_id'],'school-second-paper','school-second-revision')
        say('这次写好了，看看新的这一页','second-look','writing.read_original')
        reading2=say('这页只有我写的答案','second-scope','writing.read_original')
        assert reading2['action']['type']=='read_writing_original',reading2
        playback2=readback_receipt(client,sid,reading2,playback_id='school-second-read')
        assert client.put(f"/v1/agent-turns/{reading2['agent_turn_id']}/outcome",json={
            'final_response':'新稿已读回','tool_status':'completed','result':playback2}).status_code==200
        confirmation2=say('对，是这一稿，识别没有错','second-confirm','writing.confirm_original')
        submitted2=client.post(f"/v1/agent-turns/{confirmation2['agent_turn_id']}/execute")
        assert submitted2.status_code==200,submitted2.text
        follow2=submitted2.json()['turn']['local_evaluation']
        if formula_revision:
            assert 'formula_review_required' in page2.json()['details']['quality']['issues']
            assert follow2['status']=='local_feedback_required',follow2
            assert follow2['reason']=='WRITING_PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'
            assert len(gateway.evaluations)==1
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
            return
        assert follow2['status']=='feedback_required',json.dumps(follow2,ensure_ascii=False)
        evaluated2=client.post(f"/v1/agent-turns/{follow2['feedback_turn_id']}/execute")
        assert evaluated2.status_code==200,evaluated2.text
        assert evaluated2.json()['execution']['result']['formative_only']
        assert len(gateway.evaluations)==2
        assert json.loads(gateway.evaluations[-1].messages[1]['content'])['response']==second_text
        with db.connect() as c:
            second_row=c.execute('SELECT assessment_id,payload_json FROM rubric_assessment WHERE assessment_id!=?',(assessment_id,)).fetchone()
            second_assessment=json.loads(second_row['payload_json'])
        second_review_path=f"/v1/parent/children/{CHILD}/learning-rubrics/{second_row['assessment_id']}"
        second_detail=client.get(second_review_path,headers=headers).json()
        second_binding=second_detail['writing_review']['binding']
        # These scores are explicit fixture judgments of the synthetic solution,
        # not model-calibrated marks or evidence of a child's learning outcome.
        second_review_body={
            **human_review,'client_action_id':'second-school-review','independent':True,'hint_level':0,
            'reason':'Fixture reviewer checked the synthetic solution and its stated conditions.',
            'dimensions':[{'dimension_id':d['dimension_id'],'score':3,'response_quotes':[second_text],
                'rationale':'The synthetic solution addresses this criterion.'} for d in second_assessment['dimensions']],
            'criterion_evidence':[{'criterion':point,'response_quote':second_text} for point in second_assessment['criteria']],
            'writing_evidence':{'binding_version':second_binding['binding_version'],
                'task_point_evidence':[{'task_point':point,'response_quote':second_text} for point in second_binding['task_points']]}}
        if with_material and subject == '物理':
            missing=client.post(second_review_path+'/review',headers=headers,json=second_review_body)
            assert missing.status_code==409,missing.text
            evidence=next(d for d in second_review_body['dimensions'] if d['dimension_id']=='evidence')
            evidence['source_quotes']=['The passage contains this invented measurement.']
            invented=client.post(second_review_path+'/review',headers=headers,json=second_review_body)
            assert invented.status_code==409,invented.text
            evidence['source_quotes']=[second_assessment['material']['stimulus_text']]
        second_review=client.post(second_review_path+'/review',headers=headers,json=second_review_body)
        assert second_review.status_code==200,second_review.text
        assert second_review.json()['qualification']['eligible_for_mastery_gate']==(next_kind=='independent_check')
        recommendations=LearningOutcomeCoordinator(db).inspect_child(CHILD)
        recommendation=next(row for row in recommendations if row['objective_id']==second_assessment['objective_id'])
        assert recommendation['outcome_level']==('independent' if next_kind=='independent_check' else 'understood_now'),recommendation
        if next_kind=='independent_check':
            assert recommendation['next_phase']=='transfer_check' and recommendation['not_before']
            transition=second_review.json()['learning_transition']
            assert transition['status']=='applied' and transition['objective_phase']=='transfer_check',transition
            commitment_id=transition['commitment_id']
            with db.connect() as c:
                commitment=dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(commitment_id,)).fetchone())
                assert commitment['status']=='pending' and commitment['due_at']==recommendation['not_before']
                assert commitment['child_id']==CHILD and commitment['objective_id']==recommendation['objective_id']
        else:
            assert second_review.json()['learning_transition']['status']=='requires_evidence'
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        replay2=client.post(f"/v1/agent-turns/{follow2['feedback_turn_id']}/execute")
        assert replay2.status_code==200,replay2.text
        assert len(gateway.evaluations)==2
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==2
            assert all(row[0] is None for row in c.execute('SELECT correctness FROM diagnostic_attempt'))
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==2
        parent=client.post('/v1/parent/unlock',json={'pin':'8264'})
        headers={'X-HSC-Parent':parent.json()['parent_token']}
        old_review=client.post(second_review_path+'/review',headers=headers,json=second_review_body)
        assert old_review.status_code==200,old_review.text
        if next_kind=='independent_check':
            assert old_review.json()['learning_transition']['status']=='already_applied',old_review.json()['learning_transition']
            assert old_review.json()['learning_transition']['commitment_id']==commitment_id
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE objective_id=? AND status='pending'",
                    (recommendation['objective_id'],)).fetchone()[0]==1
        retracted=client.post(review_path+'/review',headers=headers,json={'operation':'retract','expected_revision':1,
            'client_action_id':'school-review-retract','reason':'The isolated review now requires rechecking.'})
        assert retracted.status_code==200,retracted.text
        after=ChildModelService(db).tutor_controls(CHILD,1,None,{},session_id=sid,subject=subject,
            knowledge_node_id=source['knowledge_node_id'],task_type='diagnostic')
        assert after['audit']['teaching_context']['source_version']!=controls['audit']['teaching_context']['source_version']
        assert after['audit']['teaching_context']['learning']['status']!='recheck_needed'
        if next_kind=='independent_check':
            revoke_positive=client.post(second_review_path+'/review',headers=headers,json={
                'operation':'retract','expected_revision':1,'client_action_id':'school-positive-retract',
                'reason':'Withdraw the fixture judgment; do not keep its pending transfer check.'})
            assert revoke_positive.status_code==200,revoke_positive.text
            with db.connect() as c:
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(commitment_id,)).fetchone()[0]=='cancelled'
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2

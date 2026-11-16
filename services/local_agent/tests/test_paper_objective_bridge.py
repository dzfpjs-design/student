"""Public synthetic paired originals; these tests do not approve household content."""
from copy import deepcopy
import json

import pytest

from home_study_agent.knowledge_graph.paper_objective_bridge import PaperObjectiveBridge, answer_rubric, assert_source_current
from home_study_agent.knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
from home_study_agent.database import Database
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.spoken_numeric_tuple import score
from test_curriculum_supply import prepared, CHILD
from test_parent_paper_objective_draft import capture_pair, objective
from test_parent_textbook_drafts import payload


def fixture(tmp_path):
    original_db, _, _ = prepared(tmp_path)
    db = Database(original_db.path, media_root=tmp_path, data_encryption_key=b'0'*32)
    _, pages = capture_pair(db, CHILD)
    value = payload(objective_draft={**objective(pages), 'candidate_node_id':'node-p4-math-arithmetic'})
    drafts = ParentTextbookDraftService(db)
    saved = drafts.save(CHILD,'capture',0,'public-paper-save-001',value,
                        expected_generation=drafts.read(CHILD,'capture')['storage_generation'])
    return db, drafts, saved, value


def command(options):
    return dict(seed_choice={k:options['seeds'][0][k] for k in ('diagnostic_item_id','binding')} if options['seeds'] else None,
                draft_identity=options['draft_identity'], expected_selection_version=options['selection']['version'],expected_node_binding=options['node']['binding'],
                standard_key=options['standards'][0]['key'], standard_binding=options['standards'][0]['binding'],
                answer_semantics={'kind':'whole_answer_variants'}, originals_confirmed=True,
                license_status='family_owned_index', difficulty=1,
                action_id='public-paper-submit-001')


def test_answer_meaning_is_explicit_and_ordered_parts_never_become_OR():
    value={'official_answers':['3','4']}
    assert answer_rubric(value,{'kind':'whole_answer_variants'})['exact_answers']==['3','4']
    rubric=answer_rubric(value,{'kind':'ordered_numeric_parts','numeric_kind':'integer','tuple_size':2})
    assert rubric['exact_answers']==['3,4']
    assert score(rubric,'3') is None and score(rubric,'3,4') is True and score(rubric,'4,3') is False
    for mode in ({},{'kind':'ordered_parts'},{'kind':'ordered_numeric_parts','numeric_kind':'integer','tuple_size':3}):
        with pytest.raises(ValueError):answer_rubric(value,mode)
    with pytest.raises(ValueError):answer_rubric({'official_answers':['先说明原因','再作比较']},
        {'kind':'ordered_numeric_parts','numeric_kind':'integer','tuple_size':2})


def test_submit_freezes_receipt_without_approving_candidate_or_switching_curriculum(tmp_path):
    db, drafts, saved, value=fixture(tmp_path);bridge=PaperObjectiveBridge(db)
    before=CurriculumSupplyService(db).audit(CHILD)['current_curriculum']
    options=bridge.options(CHILD);body=command(options)
    result=bridge.submit(CHILD,**body)
    assert result['state']=='awaiting_formal_validation' and result['eligible'] is False
    assert result['route']=='parallel' and result['seed_item_id']
    assert result['candidate_id'] is None and result['diagnostic_item_id'] is None
    assert CurriculumSupplyService(db).audit(CHILD)['current_curriculum']==before
    assert bridge.submit(CHILD,**body)==result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_supply_package').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        source=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(result['source_id'],)).fetchone())
        receipt=assert_source_current(c,db,CHILD,source)
        assert receipt['draft_identity']['revision']==saved['revision']
        packet=json.loads(c.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=?',(result['source_id'],)).fetchone()[0])
        assert packet['prompt']==value['objective_draft']['full_question']


def test_next_question_and_third_capture_do_not_revoke_frozen_Q6(tmp_path):
    db,drafts,saved,value=fixture(tmp_path);bridge=PaperObjectiveBridge(db)
    result=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    next_value=deepcopy(value);next_value['objective_draft']['question_number']='7'
    next_value['objective_draft']['full_question']='公开合成下一题：四加五等于多少？'
    drafts.save(CHILD,'capture',saved['revision'],'public-paper-Q7-002',next_value,
                expected_generation=saved['storage_generation'])
    capture_pair(db,'child-junior-3')
    with db.connect() as c:
        source=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(result['source_id'],)).fetchone())
        assert assert_source_current(c,db,CHILD,source)['draft_identity']['revision']==saved['revision']
    with pytest.raises(ValueError):bridge.submit(CHILD,**command({**bridge.options(CHILD),'draft_identity':
        {'revision':saved['revision'],'storage_generation':saved['storage_generation'],'last_action_id':saved['last_action_id']}}))


@pytest.mark.parametrize('changed',['paper','answer_key','generation','foreign','no_confirmation','standard','node'])
def test_submission_rejects_stale_or_unconfirmed_without_creating_source(tmp_path,changed):
    db,drafts,saved,value=fixture(tmp_path);bridge=PaperObjectiveBridge(db);body=command(bridge.options(CHILD))
    with db.connect() as c:
        count=c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]
        if changed in ('paper','answer_key'):
            from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
            ref=value['objective_draft'][changed]['capture_ref'];cap=ParentTextbookCaptureService(db,None)
            c.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='skipped' WHERE artifact_id=?",(cap._id_for_ref(ref),))
        elif changed=='generation':c.execute("UPDATE privacy_storage_generation SET generation='new-generation'")
        elif changed=='node':c.execute("UPDATE knowledge_node SET knowledge_version='changed-version' WHERE knowledge_node_id='node-p4-math-arithmetic'")
        elif changed=='standard':c.execute("UPDATE curriculum_standard_page SET content_text='changed' WHERE source_id='std'")
        c.commit()
    if changed=='no_confirmation':body['originals_confirmed']=False
    with pytest.raises(ValueError):bridge.submit('child-junior-3' if changed=='foreign' else CHILD,**body)
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]==count


def test_supplement_uses_existing_formal_state_machine_without_reselect(tmp_path):
    db,drafts,saved,value=fixture(tmp_path);bridge=PaperObjectiveBridge(db)
    sent=bridge.submit(CHILD,**command(bridge.options(CHILD)))
    service=CurriculumSupplyService(db)
    candidate=service.generate_draft(CHILD,sent['source_spec'],author='local:reviewed_source_extractor_v1',
        action_id='public-paper-generate-001',expected_scope_version=sent['expected_scope_version'])
    assert candidate['state']=='draft',candidate
    cid=candidate['candidate_id']
    checked=service.validate(CHILD,cid,actor='local:curriculum_validator',action_id='public-paper-validate-001',expected_version=1)
    assert checked['state']=='validated',checked
    assert not checked['eligible']
    reviewed=service.review(CHILD,cid,actor='parent',decision='approve',note='逐项核对公开合成两原页、完整答案与节点课标。',
        action_id='public-paper-review-001',expected_version=2)
    assert reviewed['state']=='reviewed' and not reviewed['eligible']
    published=service.publish(CHILD,cid,actor='parent',action_id='public-paper-publish-001',expected_version=3)
    assert published['eligible'],published
    next_value=deepcopy(value);next_value['objective_draft']['question_number']='7'
    drafts.save(CHILD,'capture',saved['revision'],'public-paper-save-Q7',next_value,expected_generation=saved['storage_generation'])
    with db.connect() as c:assert validate_item(c,CHILD,published['diagnostic_item_id'])
    from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
    cap=ParentTextbookCaptureService(db,None)
    with db.connect() as c:
        c.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='skipped' WHERE artifact_id=?",(cap._id_for_ref(value['objective_draft']['answer_key']['capture_ref']),));c.commit()
        with pytest.raises(LocalSupplyError):validate_item(c,CHILD,published['diagnostic_item_id'])


@pytest.mark.parametrize('ordered',[False,True],ids=['whole-answer','ordered-numeric'])
def test_real_HTTP_formal_review_Tutor_is_not_attempt_then_independent_answer(tmp_path,ordered):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelResult
    class RecordingGateway:
        configured=True
        def __init__(self):self.requests=[]
        async def complete(self,request):
            self.requests.append(request)
            return ModelResult(content='先读题目中的已知条件，你能说出一个吗？',provider='synthetic',model='synthetic-Tutor',latency_ms=1)
    db,drafts,saved,value=fixture(tmp_path)
    value['objective_draft']['full_question']='公开合成原题：分别计算一加二、二加二，按顺序作答。' if ordered else '公开合成原题：十二加五等于多少？'
    value['objective_draft']['official_answers']=['3','4'] if ordered else ['17','十七']
    saved=drafts.save(CHILD,'capture',saved['revision'],'public-paper-HTTP-save',value,expected_generation=saved['storage_generation'])
    settings=Settings(data_dir=tmp_path,database_path=db.path,data_encryption_key=b'0'*32,maintenance_enabled=False,session_token='public-paired-paper')
    gateway=RecordingGateway();base=f'/v1/parent/knowledge-graph/textbook-capture/{CHILD}/objective-review'
    formal=f'/api/parent/children/{CHILD}/curriculum-supply'
    with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as client:
        assert client.get(base+'/options').status_code==401
        client.headers['X-HSC-Parent']=client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        options=client.get(base+'/options');assert options.status_code==200,options.text
        body=command(options.json())
        if ordered:body['answer_semantics']={'kind':'ordered_numeric_parts','numeric_kind':'integer','tuple_size':2}
        sent=client.post(base,json=body);assert sent.status_code==200,sent.text
        sent=sent.json()
        assert client.post(base,json=body).json()==sent
        assert client.get(base+'/'+sent['receipt_id']).json()==sent
        response=client.post(base+'/'+sent['receipt_id']+'/generate',json={'expected_generation':saved['storage_generation']})
        assert response.status_code==200,response.text
        cid=response.json()['candidate']['candidate_id']
        assert client.post(base+'/'+sent['receipt_id']+'/generate',json={'expected_generation':saved['storage_generation']}).json()['candidate']['candidate_id']==cid
        for version,operation in enumerate(('validate','review','publish'),1):
            request={'action_id':'public-paired-'+operation,'expected_version':version}
            if operation=='review':request.update(decision='approve',note='核对两公开合成原页、题干、完整答案含义、知识点与课标。')
            result=client.post(formal+f'/{cid}/{operation}',json=request)
            assert result.status_code==200,result.text
            assert result.json()['state']=={'validate':'validated','review':'reviewed','publish':'published'}[operation],result.text
        item=result.json()['diagnostic_item_id']
        assert result.json()['eligible']
        sid=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'public-paired-session'}).json()['session_id']
        root=f'/v1/sessions/{sid}'
        response=client.post(root+'/diagnostic-prompt/prepare',json={'track':'school','preferred_item_id':item,'client_action_id':'public-paired-question'})
        assert response.status_code==200,response.text
        prompt=response.json()['prompt']
        assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':True}).status_code==200
        response=client.post(root+'/tutor',json={'subject':'数学','question':prompt['item']['prompt'],'prompt_id':prompt['prompt_id'],
            'item_version':prompt['item_version'],'client_action_id':'public-paired-tutor'})
        assert response.status_code==200,response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0]==1
        # A Tutor-exposed question cannot be called an independent assessment.
        assert client.post(root+'/diagnostic-prompt/cancel',json={'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version']}).status_code==200
        next_value=deepcopy(value);next_value['objective_draft']['question_number']='7'
        next_value['objective_draft']['full_question']='公开合成新原题：分别计算一加二、二加三，按顺序作答。' if ordered else '公开合成新原题：十二加六等于多少？'
        next_value['objective_draft']['official_answers']=['3','5'] if ordered else ['18','十八']
        current=drafts.read(CHILD,'capture')
        drafts.save(CHILD,'capture',current['revision'],'public-paired-Q7-save',next_value,expected_generation=current['storage_generation'])
        next_body=command(client.get(base+'/options').json());next_body['action_id']='public-paired-Q7-submit'
        if ordered:next_body['answer_semantics']=body['answer_semantics']
        second=client.post(base,json=next_body);assert second.status_code==200,second.text
        second=second.json()
        q7=client.post(formal+'/generate',json={'action_id':'public-paired-Q7-generate','expected_version':0,
            'expected_scope_version':second['expected_scope_version'],'source':second['source_spec']})
        assert q7.status_code==200,q7.text
        q7cid=q7.json()['candidate_id']
        for version,operation in enumerate(('validate','review','publish'),1):
            request={'action_id':'public-Q7-'+operation,'expected_version':version}
            if operation=='review':request.update(decision='approve',note='核对第二公开合成原题的完整题干、答案与原页。')
            q7=client.post(formal+f'/{q7cid}/{operation}',json=request)
            assert q7.status_code==200 and q7.json()['state']=={'validate':'validated','review':'reviewed','publish':'published'}[operation],q7.text
        assert client.get(formal+'/'+cid).json()['eligible']
        q7item=q7.json()['diagnostic_item_id']
        prepared=client.post(root+'/diagnostic-prompt/prepare',json={'track':'school','preferred_item_id':q7item,
            'require_unexposed':True,'client_action_id':'public-paired-Q7-independent'})
        assert prepared.status_code==200,prepared.text
        prompt=prepared.json()['prompt']
        response=client.post(root+'/diagnostic-prompt/answer',json={'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
            'answer':'3,6' if ordered else '12','client_action_id':'public-paired-independent-wrong'})
        assert response.status_code==200,response.text
        attempt=response.json()['attempt']
        assert attempt['correctness']!=1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM mistake_case').fetchone()[0]==1
            rows=c.execute('SELECT * FROM review_task WHERE child_id=?',(CHILD,)).fetchall()
            assert rows and all(row['source_evidence_id'] for row in rows)
            from datetime import datetime,timedelta
            delayed={row['interval_label']:row for row in rows if row['interval_label'] in {'48h','7d'}}
            assert set(delayed)=={'48h','7d'}
            for label,days in [('48h',2),('7d',7)]:
                assert datetime.fromisoformat(delayed[label]['due_at'])-datetime.fromisoformat(delayed[label]['created_at'])==timedelta(days=days)
                assert delayed[label]['status']=='cancelled'  # real mistake flow owns remediation, not generic reviews
            assert c.execute('SELECT delayed_48h_due_at FROM mistake_mastery_gate').fetchone() is None
            # Diagnosis/remediation and a genuine future due are outstanding; do not forge them.
        # Cause is a candidate, not a fabricated confirmed diagnosis. All writes
        # use the real HTTP interfaces; no SQL inserts grant stage/evidence authority.
        with db.connect() as c:mid=c.execute('SELECT mistake_id FROM mistake_case').fetchone()[0]
        gateway.configured=False  # this deterministic model double must not launch a provider/variant worker
        diagnosed=client.post(f'/v1/mistakes/{mid}/diagnose',json={'child_id':CHILD,'error_type':'retrieval_failure',
            'explanation':'公开合成错因候选：需要重新回忆基本加法规则；尚未证明因果。','confidence':.5,'client_action_id':'public-cause-candidate'})
        assert diagnosed.status_code==200,diagnosed.text
        assert diagnosed.json()['diagnosis']['status']=='candidate'
        hypotheses=client.post(f'/v1/mistakes/{mid}/diagnosis/hypotheses',json={'child_id':CHILD,'expected_version':0,
            'client_action_id':'public-cause-hypotheses','hypotheses':[{'error_type':'retrieval_failure','stance':'candidate',
                'explanation':'需通过后续独立观察检验，不能从一次错答认定。'}]})
        assert hypotheses.status_code==200,hypotheses.text
        assert hypotheses.json()['assessment']['causal_status']=='unconfirmed'
        gateway.configured=True  # HTTP Tutor uses only the explicit local test double; model quality is not validated
        explained=client.post(root+'/tutor',json={'subject':'数学','question':prompt['item']['prompt'],'prompt_id':prompt['prompt_id'],
            'item_version':prompt['item_version'],'hint_level':2,'client_action_id':'public-paired-remediation-Tutor'})
        assert explained.status_code==200,explained.text
        gateway.configured=False
        remediated=client.post(f'/v1/mistakes/{mid}/remediation-steps',json={'child_id':CHILD,'step_type':'active_recall',
            'outcome':'completed','notes':'公开合成讲解回执已保存，尚不认定理解或掌握，继续独立新题。','client_action_id':'public-remediation-step'})
        assert remediated.status_code==200,remediated.text

        def author_original(number,question,answers,stage=None):
            current=client.get(f'/v1/parent/knowledge-graph/textbook-capture/{CHILD}/drafts/capture').json()['draft']
            editing=deepcopy(current['payload']);editing['objective_draft'].update(question_number=str(number),full_question=question,official_answers=answers)
            saved_response=client.put(f'/v1/parent/knowledge-graph/textbook-capture/{CHILD}/drafts/capture',json={
                'expected_revision':current['revision'],'expected_generation':current['storage_generation'],
                'client_action_id':f'public-original-Q{number}-save','payload':editing})
            assert saved_response.status_code==200,saved_response.text
            o=client.get(base+'/options').json();b=command(o);b['action_id']=f'public-original-Q{number}-submit'
            b['seed_choice']={k:next(v for v in o['seeds'] if v['diagnostic_item_id']==q7item)[k] for k in ('diagnostic_item_id','binding')}
            if ordered:b['answer_semantics']=body['answer_semantics']
            source=client.post(base,json=b);assert source.status_code==200,source.text
            source=source.json();spec=source['source_spec'];assert spec['seed_item_id']==q7item
            result=client.post(formal+'/generate',json={'action_id':f'public-original-Q{number}-generate','expected_version':0,
                'expected_scope_version':source['expected_scope_version'],'source':spec})
            assert result.status_code==200,result.text
            candidate_id=result.json()['candidate_id']
            checked=client.post(formal+f'/{candidate_id}/validate',json={'action_id':f'public-original-Q{number}-validate','expected_version':1})
            assert checked.status_code==200 and checked.json()['state']=='validated',checked.text
            review={'action_id':f'public-original-Q{number}-review','expected_version':2,'decision':'approve',
                    'note':'核对公开合成原题、完整参考答案以及与原错题相同节点；不是孩子掌握证明。'}
            if stage:
                context=checked.json()['stage_review_context']
                review['stage_review']={'schema':context['schema'],'stage':stage,'binding_version':context['binding_version'],
                    'source_quote':context['source_prompt'],'candidate_quote':context['candidate_prompt'],
                    'same_skill_rationale':'两份公开合成原题都要求按同一基本加法规则准确计算。',
                    'changed_context_rationale':'新原题使用不同数字与独立问题，尚未在孩子界面讲解或作答。'}
            reviewed=client.post(formal+f'/{candidate_id}/review',json=review)
            assert reviewed.status_code==200 and reviewed.json()['state']=='reviewed',reviewed.text
            published=client.post(formal+f'/{candidate_id}/publish',json={'action_id':f'public-original-Q{number}-publish','expected_version':3})
            assert published.status_code==200 and published.json()['eligible'],published.text
            return published.json()['diagnostic_item_id'],candidate_id

        repair_item,_=author_original(8,'公开合成修复独立原题：分别计算一加三、二加三，按顺序作答。' if ordered else '公开合成修复独立原题：十二加七等于多少？',['4','5'] if ordered else ['19'])
        repair=client.post(f'/v1/mistakes/{mid}/attempts',json={'child_id':CHILD,'stage':'repair_check','diagnostic_item_id':repair_item,
            'answer':'4,5' if ordered else '19','hint_level':0,'client_action_id':'public-repair-independent'})
        assert repair.status_code==200,repair.text
        assert repair.json()['mastery_gate']['satisfied']['repair_check']==1,repair.text
        near_item,near_cid=author_original(9,'公开合成近迁移原题：分别计算一加三、二加四，按顺序作答。' if ordered else '公开合成近迁移原题：十二加八等于多少？',['4','6'] if ordered else ['20'],'near_transfer')
        near=client.post(f'/v1/mistakes/{mid}/attempts',json={'child_id':CHILD,'stage':'near_transfer','diagnostic_item_id':near_item,
            'answer':'4,6' if ordered else '20','hint_level':0,'client_action_id':'public-near-independent'})
        assert near.status_code==200,near.text
        far_item,far_cid=author_original(10,'公开合成远迁移原题：两组分别有五与七张纸，请按先后报出每组的数量。' if ordered else '公开合成远迁移原题：书架有十四本书又放入七本，现在几本？',['5','7'] if ordered else ['21'],'far_transfer')
        far=client.post(f'/v1/mistakes/{mid}/attempts',json={'child_id':CHILD,'stage':'far_transfer','diagnostic_item_id':far_item,
            'answer':'5,7' if ordered else '21','hint_level':0,'client_action_id':'public-far-independent'})
        # This mistake's current gate needs near, then timed retention; a valid
        # far source does not grant permission to skip or repeat gate stages.
        assert far.status_code==409,far.text
        gate=near.json()['mastery_gate'];assert gate['satisfied']['near_transfer']==1
        # Negative-only adversarial mutations are rolled back. No SQL creates a
        # qualification, diagnosis, approval or passing response in this flow.
        from home_study_agent.learning_obligations import qualified_exact_score,project_child_mastery_evidence
        with db.connect() as c:
            scope={'stage':'near_transfer','mistake_id':mid}
            assert validate_item(c,CHILD,near_item,**scope)
            assert validate_item(c,CHILD,far_item,stage='far_transfer',mistake_id=mid)
            for rejected_item,rejected_stage,owner in ((repair_item,'near_transfer',CHILD),(far_item,'near_transfer',CHILD),
                    (near_item,'representation_transfer',CHILD),(near_item,'discrimination',CHILD),(near_item,'near_transfer','child-junior-3')):
                with pytest.raises(LocalSupplyError):validate_item(c,owner,rejected_item,stage=rejected_stage,mistake_id=mid)
            evidence=c.execute("SELECT evidence_id FROM mistake_attempt WHERE client_action_id='public-near-independent'").fetchone()[0]
            candidate=c.execute('SELECT * FROM specialized_supply_candidate WHERE candidate_id=?',(near_cid,)).fetchone()
            validation=json.loads(candidate['validation_json']);validation.pop('stage_review')
            package=json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',(near_cid,)).fetchone()[0])
            for sql,args in [
                ("UPDATE mistake_case SET diagnostic_item_id=? WHERE mistake_id=?",(item,mid)),
                ("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?",(mid,)),
                ("UPDATE specialized_supply_candidate SET reviewed_at='2999-01-01T00:00:00+00:00' WHERE candidate_id=?",(near_cid,)),
                ("UPDATE specialized_supply_candidate SET review_action_id='local-qualification:not-parent' WHERE candidate_id=?",(near_cid,)),
                ("UPDATE specialized_supply_candidate SET validation_json=? WHERE candidate_id=?",(json.dumps(validation),near_cid)),
                ("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?",(package['source']['source_id'],)),
                ("UPDATE knowledge_source SET content_hash=? WHERE source_id=?",('0'*64,package['source']['source_id']))]:
                c.execute('SAVEPOINT denied_source');c.execute(sql,args)
                with pytest.raises(LocalSupplyError):validate_item(c,CHILD,near_item,**scope)
                assert qualified_exact_score(c,CHILD,near_item,'4,6' if ordered else '20',**scope) is None
                projection=project_child_mastery_evidence(c,CHILD)['node-p4-math-arithmetic']
                assert any(row['evidence_id']==evidence for row in projection['excluded_evidence'])
                c.execute('ROLLBACK TO denied_source');c.execute('RELEASE denied_source')
            assert validate_item(c,CHILD,near_item,**scope)
        assert gate['missing']==['delayed_48h','delayed_7d']
        with db.connect() as c:
            latest=c.execute("SELECT created_at FROM mistake_attempt WHERE client_action_id='public-near-independent'").fetchone()[0]
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        from datetime import datetime,timedelta
        assert datetime.fromisoformat(gate['delayed_48h_due_at'])==datetime.fromisoformat(latest)+timedelta(hours=48)
        assert datetime.fromisoformat(gate['delayed_7d_due_at'])==datetime.fromisoformat(latest)+timedelta(days=7)
        scheduled=client.get(f'/v1/mistakes/{mid}/next',params={'child_id':CHILD})
        assert scheduled.status_code==200 and scheduled.json()['kind']=='scheduled'
        assert scheduled.json()['stage']=='delayed_48h'
        early=client.post(f'/v1/mistakes/{mid}/attempts',json={'child_id':CHILD,'stage':'delayed_48h','diagnostic_item_id':near_item,
            'answer':'4,6' if ordered else '20','hint_level':0,'client_action_id':'public-delayed-too-early'})
        assert early.status_code==409,early.text
        final=sent['receipt_id']
    with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41201),headers={'X-HSC-Session':settings.session_token}) as client:
        client.headers['X-HSC-Parent']=client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        assert client.get(base+'/'+final).status_code==200
        assert client.get(formal+'/'+cid).json()['eligible']
        persisted=client.get(f'/v1/mistakes/{mid}/next',params={'child_id':CHILD})
        assert persisted.json()==scheduled.json()
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0]==2


def test_complete_portable_restore_requalifies_frozen_source_but_rejects_old_operation(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    source_root=tmp_path/'source';source_root.mkdir()
    db,drafts,saved,value=fixture(source_root);bridge=PaperObjectiveBridge(db);body=command(bridge.options(CHILD))
    sent=bridge.submit(CHILD,**body);service=CurriculumSupplyService(db)
    row=service.generate_draft(CHILD,sent['source_spec'],author='local:reviewed_source_extractor_v1',
        action_id='public-portable-generate',expected_scope_version=sent['expected_scope_version'])
    cid=row['candidate_id']
    service.validate(CHILD,cid,actor='local:curriculum_validator',action_id='public-portable-validate',expected_version=1)
    service.review(CHILD,cid,actor='parent',decision='approve',note='公开合成完整双页与答案审核。',action_id='public-portable-review',expected_version=2)
    published=service.publish(CHILD,cid,actor='parent',action_id='public-portable-publish',expected_version=3)
    archive=PrivacyLifecycleService(db,source_root,b'0'*32).create_portable_backup('public synthetic portable backup passphrase')['path']
    target=tmp_path/'target';target.mkdir(mode=0o700);target_db=Database(target/'agent.db',media_root=target,data_encryption_key=b'n'*32);target_db.initialize()
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from privacy_restore_operation import restore_operation
    from test_companion_activity_host import NoCloud
    settings = Settings(data_dir=target,database_path=target_db.path,data_encryption_key=b'n'*32,
        native_launch_attestation_key=None,maintenance_enabled=False,agent_runtime_enabled=False,
        session_token='public-objective-portable')
    with TestClient(create_app(settings,model_gateway=NoCloud()),headers={'X-HSC-Session':settings.session_token})as client:
        setup=client.post('/v1/parent/setup',json={'pin':'4826'});assert setup.status_code==200,setup.text
        headers={'X-HSC-Parent':setup.json()['parent_token']}
        operation=restore_operation(client,headers=headers)
        response=client.post('/v1/parent/backups/portable/restore',headers=headers,json={'path':archive,
            'passphrase':'public synthetic portable backup passphrase','confirmation':'确认迁移恢复','operation':operation})
        assert response.status_code==200,response.text
        result=response.json()
    assert result['status']=='restored'
    restored=PaperObjectiveBridge(target_db)
    assert restored.read(CHILD,sent['receipt_id'])==sent
    with target_db.connect() as c:assert validate_item(c,CHILD,published['diagnostic_item_id'])
    with pytest.raises(ValueError):restored.submit(CHILD,**body)
    with pytest.raises(ValueError):restored.generate(CHILD,sent['receipt_id'],expected_generation=body['draft_identity']['storage_generation'])
    with db.connect() as c:
        audit=c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',(sent['receipt_id'],)).fetchone()[0]
        assert value['objective_draft']['full_question'] not in audit
        assert 'private parent draft' not in audit and 'standard_quote' not in audit
        assert 'description' not in audit and 'scope_json' not in audit


def test_existing_unapproved_same_content_is_not_relabelled_or_adopted(tmp_path):
    from home_study_agent.knowledge_base import KnowledgeBaseService
    db,drafts,saved,value=fixture(tmp_path);bridge=PaperObjectiveBridge(db)
    packet=dict(schema='curriculum_objective_source_v1',subject='数学',grade='四年级',knowledge_node_id='node-p4-math-arithmetic',
        assessment_mode='objective',answer_type='text',prompt=value['objective_draft']['full_question'],
        expected_answer={'local_rule':'local_exact_v1','exact_answers':['17']})
    content=json.dumps(packet,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    existing=KnowledgeBaseService(db).import_source(CHILD,dict(source_type='family_note',title='公开合成但尚未审核来源',
        subject='数学',grade_band='四年级',version_label='other',source_ref='public-other-origin',license_status='unknown',parent_reviewed=False,content_text=content))
    with pytest.raises(LocalSupplyError,match='EXISTING_SOURCE_REVIEW_REQUIRED'):bridge.submit(CHILD,**command(bridge.options(CHILD)))
    with db.connect() as c:
        row=c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(existing['source_id'],)).fetchone()
        assert row['review_status']=='quarantined' and row['source_ref']=='public-other-origin' and row['license_status']=='unknown'
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_paired_paper_objective_submitted'").fetchone()[0]==0


def test_no_seed_uses_first_source_then_exact_qualified_original_reuses_version(tmp_path):
    db,drafts,saved,value=fixture(tmp_path)
    with db.connect() as c:
        c.execute("DELETE FROM diagnostic_item WHERE knowledge_node_id='node-p4-math-arithmetic'");c.commit()
    bridge=PaperObjectiveBridge(db);body=command(bridge.options(CHILD));sent=bridge.submit(CHILD,**body)
    assert sent['route']=='first_source' and sent['seed_item_id'] is None
    generated=bridge.generate(CHILD,sent['receipt_id'],expected_generation=saved['storage_generation'])['candidate']
    cid=generated['candidate_id'];assert cid.startswith('first-')
    service=CurriculumSupplyService(db)
    service.validate(CHILD,cid,actor='local:curriculum_validator',action_id='public-first-validate',expected_version=1)
    service.review(CHILD,cid,actor='parent',decision='approve',note='公开合成原题完整核对，不声称独立迁移或孩子掌握。',action_id='public-first-review',expected_version=2)
    published=service.publish(CHILD,cid,actor='parent',action_id='public-first-publish',expected_version=3)
    assert published['eligible']
    with db.connect() as c:count=c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]
    with pytest.raises(LocalSupplyError,match='ORIGIN_REQUIRED'):
        bridge.submit(CHILD,**{**body,'action_id':'public-exact-reuse-old-selection'})
    reused=bridge.submit(CHILD,**{**command(bridge.options(CHILD)),'action_id':'public-exact-reuse-001'})
    assert reused['route']=='reuse' and reused['diagnostic_item_id']==published['diagnostic_item_id'] and reused['source_id'] is None
    assert bridge.generate(CHILD,reused['receipt_id'],expected_generation=saved['storage_generation'])['candidate'] is None
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]==count


def test_published_original_survives_real_OCR_metadata_actions_but_old_authoring_is_rejected(tmp_path):
    from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
    db,drafts,saved,value=fixture(tmp_path);bridge=PaperObjectiveBridge(db);body=command(bridge.options(CHILD))
    sent=bridge.submit(CHILD,**body);service=CurriculumSupplyService(db)
    created=bridge.generate(CHILD,sent['receipt_id'],expected_generation=saved['storage_generation'])['candidate'];cid=created['candidate_id']
    service.validate(CHILD,cid,actor='local:curriculum_validator',action_id='public-ocr-check',expected_version=1)
    service.review(CHILD,cid,actor='parent',decision='approve',note='完整公开合成双原页、题干答案、节点课标已核对。',action_id='public-ocr-review',expected_version=2)
    published=service.publish(CHILD,cid,actor='parent',action_id='public-ocr-publish',expected_version=3)
    capture=ParentTextbookCaptureService(db,None)
    for role in ('paper','answer_key'):
        ref=value['objective_draft'][role]['capture_ref']
        corrected=capture.review(CHILD,ref,'公开合成OCR元数据校正，不改变已认证原图或原题证据。','public-ocr-correct-'+role)
        assert corrected['source_version']!=value['objective_draft'][role]['source_version']
        with db.connect() as c:assert validate_item(c,CHILD,published['diagnostic_item_id'])
        assert bridge.read(CHILD,sent['receipt_id'])==sent
        with pytest.raises(LocalSupplyError):bridge.submit(CHILD,**{**body,'action_id':'public-ocr-stale-'+role})
        capture.undo_review(CHILD,ref,'public-ocr-undo-'+role)
        with db.connect() as c:assert validate_item(c,CHILD,published['diagnostic_item_id'])
    capture.skip(CHILD,value['objective_draft']['answer_key']['capture_ref'],'public-ocr-withdraw')
    with db.connect() as c:
        with pytest.raises(LocalSupplyError):validate_item(c,CHILD,published['diagnostic_item_id'])


def test_origin_is_explicit_and_current_and_source_plus_receipt_roll_back_together(tmp_path):
    import sqlite3
    db,_,_,_=fixture(tmp_path);bridge=PaperObjectiveBridge(db);options=bridge.options(CHILD);body=command(options)
    assert options['seeds']
    with db.connect() as c:before=c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]
    for selection in (None,{'diagnostic_item_id':'foreign-or-missing','binding':'0'*64},
                      {**body['seed_choice'],'binding':'0'*64}):
        with pytest.raises(LocalSupplyError,match='ORIGIN_REQUIRED'):bridge.submit(CHILD,**{**body,'seed_choice':selection})
    with db.connect() as c:
        c.execute("CREATE TRIGGER public_synthetic_receipt_failure BEFORE INSERT ON audit_event WHEN NEW.action='parent_paired_paper_objective_submitted' BEGIN SELECT RAISE(ABORT,'synthetic receipt persistence failure'); END");c.commit()
    with pytest.raises(sqlite3.IntegrityError):bridge.submit(CHILD,**body)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]==before
        assert not c.execute("SELECT 1 FROM audit_event WHERE action='parent_paired_paper_objective_submitted'").fetchone()
        c.execute('DROP TRIGGER public_synthetic_receipt_failure');c.commit()
    sent=bridge.submit(CHILD,**body)
    listed=bridge.submissions(CHILD)['items']
    assert listed[0]['title']=='单题原页核对 · 6' and listed[0]['state']=='awaiting_formal_validation'
    assert set(listed[0])=={'audit_id','occurred_at','title','state'}
    bridge.generate(CHILD,sent['receipt_id'],expected_generation=body['draft_identity']['storage_generation'])
    assert bridge.submissions(CHILD)['items'][0]['state']=='draft'

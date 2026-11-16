"""Source-first open tasks use disposable authored data, never a model or family DB."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.curriculum_first_source import CurriculumFirstSourceService, VALIDATOR
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.open_response_source import SCHEMA, MATERIAL_SCHEMA
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import WritingOriginalService
from test_curriculum_first_source_http import BASE, CHILD
from test_curriculum_first_source_reliability import client_for
from test_open_response_supply_http import prepared


FAMILIES = {'数学':'mathematical_process', '语文':'textual_interpretation',
    '英语':'communicative_writing', '物理':'scientific_explanation',
    '化学':'scientific_explanation', '历史':'historical_explanation', '道德与法治':'civic_argument'}
NODE = 'synthetic-school-first-open-skill'


def first_open_case(tmp_path, *, subject='数学', mode='written', materials=False, variants=False):
    db, supply, seeded, _, _ = prepared(tmp_path, '数学')
    with db.connect() as c:
        c.execute('''INSERT INTO knowledge_node VALUES(?,'skill','school',?,'初三',
            'SYNTHETIC.FIRST.OPEN','Synthetic original reasoning skill',
            'Isolated source-first reasoning, no prior diagnostic.','isolated:first-open','test-v1','active')''', (NODE,subject))
        c.execute("UPDATE curriculum_standard_requirement SET subject_title=? WHERE requirement_id='open-requirement'", (subject,))
        c.execute("UPDATE curriculum_standard_source SET subject_title=? WHERE source_id='open-standard'", (subject,))
        c.commit()
    original = dict(schema=SCHEMA,subject=subject,grade='初三',knowledge_node_id=NODE,
        answer_type='open_response',assessment_mode='open_response_rubric',task_family=FAMILIES[subject],
        response_mode=mode,prompt='请根据题给条件组织完整推理，说明结论与依据之间的联系以及适用条件。',
        task_points=['回应题中问题并指出题给依据','解释依据如何支持结论，不补造条件'],context_requirement='self_contained')
    kb = KnowledgeBaseService(db)
    if materials:
        from home_study_agent.open_response_materials import bind_material_excerpt
        material = kb.import_source(CHILD,dict(source_type='school_material',subject=subject,grade_band='初三',
            title='Synthetic given conditions',version_label='given-v1',source_ref='isolated:first-open-given',
            license_status='public',parent_reviewed=True,content_text='两个实验除导体长度外，材料、横截面积和温度都相同。'))
        with db.connect() as c:
            excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(material['source_id'],)).fetchone()[0]
            binding = bind_material_excerpt(c,child_id=CHILD,excerpt_id=excerpt,material_key='given')
        original.update(schema=MATERIAL_SCHEMA,context_requirement='external_material',
            material_semantics='text_only',material_bindings=[binding])
    originals = [original]
    if variants:
        originals.extend([{**original,'prompt':prompt} for prompt in (
            '分析 x²-4x+4=0 在实数范围内的解，并写出判别依据和完整推理过程。',
            '一个二次函数的图像与横轴没有交点，说明系数间应满足的条件，用判别式支持你的论证。')])
    source = kb.import_source(CHILD,dict(source_type='school_material',subject=subject,grade_band='初三',
        title='Synthetic first original open question',version_label='first-open-v1',source_ref='isolated:first-open',
        license_status='public',parent_reviewed=True,content_text='\n'.join(json.dumps(v,ensure_ascii=False) for v in originals)))
    with db.connect() as c:
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence',(source['source_id'],)).fetchone()[0]
        assert not c.execute('SELECT 1 FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()
    supply.select_curriculum(CHILD,dict(grade='初三',subject=subject,edition='first-open-v1',semester='上学期',
        standard_scope='第四学段',textbook_source_id=source['source_id']),actor='parent',
        action_id='first-open-select',expected_version=1 if subject=='数学' else 0)
    spec = {k:v for k,v in seeded.items() if k not in {'seed_item_id','stage','validator'}}
    spec.update(knowledge_node_id=NODE,excerpt_id=excerpt)
    return db,source,spec,original,2 if subject=='数学' else 1


def publish_first_open(db,spec,scope_version):
    service = CurriculumFirstSourceService(db)
    draft = service.generate(CHILD,spec,action_id='first-open-draft',expected_scope_version=scope_version)
    assert draft['payload']['seed_item_id'] is None and draft['diagnostic_item_id'] is None
    assert not draft['eligible']
    cid = draft['candidate_id']
    service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='first-open-check',expected_version=1)
    service.transition(CHILD,cid,'review',actor='parent',action_id='first-open-review',expected_version=2,
        values=dict(decision='approve',note='核对合成原题、量表标准、学科能力和课标；不认证迁移或掌握。'))
    return service.transition(CHILD,cid,'publish',actor='parent',action_id='first-open-publish',expected_version=3)


@pytest.mark.parametrize('subject',FAMILIES)
@pytest.mark.parametrize('mode',['written','spoken_explanation'])
def test_zero_seed_open_task_is_visible_and_http_publishable(tmp_path,subject,mode):
    db,source,spec,original,scope = first_open_case(tmp_path,subject=subject,mode=mode)
    client = client_for(db)
    options = client.get(BASE+f'/sources/{source["source_id"]}/options')
    assert options.status_code == 200,options.text
    assert any(v['knowledge_node_id']==NODE and v['excerpt_id']==spec['excerpt_id']
        for v in options.json()['first_sources'])
    body = dict(action_id='http-first-open',expected_version=0,expected_scope_version=scope,source=spec)
    response = client.post(BASE+'/first-sources',json=body)
    assert response.status_code == 200,response.text
    cid = response.json()['candidate_id']
    assert response.json()['payload']['open_response']==original
    assert client.post(BASE+'/first-sources',json=body).json()['candidate_id']==cid
    for version,operation in enumerate(('validate','review','publish'),1):
        command = dict(action_id='http-first-'+operation,expected_version=version)
        if operation=='review': command.update(decision='approve',note='原题与参考合同核对，不作能力认证。')
        response = client.post(BASE+f'/{cid}/{operation}',json=command)
        assert response.status_code == 200,response.text
        assert response.json()['state']=={'validate':'validated','review':'reviewed','publish':'published'}[operation]
    result = response.json()
    assert result['eligible'] and result['review_packet']['open_response']==original
    with Database(db.path).connect() as c:
        item = validate_item(c,CHILD,result['diagnostic_item_id'])
        assert item['answer_type']=='open_response' and item['assessment_mode']=='open_response_rubric'
        assert not {'exact_answers','keywords','local_rule'} & json.loads(item['expected_answer_json']).keys()
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0


@pytest.mark.parametrize('fault',['source','scope','node','standard','item'])
def test_first_open_live_proof_withdrawal_and_other_child_guards(tmp_path,fault):
    db,source,spec,_,scope = first_open_case(tmp_path)
    published = publish_first_open(db,spec,scope)
    cid,item_id = published['candidate_id'],published['diagnostic_item_id']
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='初三' WHERE child_id='child-primary-4'")
        with pytest.raises(LocalSupplyError): validate_item(c,'child-primary-4',item_id)
        if fault=='source': c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?",(source['source_id'],))
        elif fault=='scope': c.execute('UPDATE curriculum_supply_selection SET version=version+1 WHERE child_id=?',(CHILD,))
        elif fault=='node': c.execute("UPDATE knowledge_node SET title='changed' WHERE knowledge_node_id=?",(NODE,))
        elif fault=='standard': c.execute("UPDATE curriculum_standard_requirement SET maturity='model_drafted' WHERE requirement_id='open-requirement'")
        else: c.execute("UPDATE diagnostic_item SET answer_type='text',assessment_mode='objective' WHERE diagnostic_item_id=?",(item_id,))
        c.commit()
    restored = CurriculumFirstSourceService(Database(db.path))
    assert not restored.get(CHILD,cid)['eligible']
    with db.connect() as c, pytest.raises(LocalSupplyError): validate_item(c,CHILD,item_id)


def test_first_open_contract_cannot_bypass_owned_context_or_claim_transfer(tmp_path):
    from home_study_agent.open_response_source import check,check_reference
    db,_,spec,source,scope = first_open_case(tmp_path)
    from home_study_agent.open_response_source import VALIDATOR as OPEN_VALIDATOR
    payload = dict(spec,seed_item_id=None,stage='parallel',validator=OPEN_VALIDATOR,
        prompt=source['prompt'],expected_answer=None,open_response=source)
    with db.connect() as c:
        node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(NODE,)).fetchone())
        with pytest.raises(LocalSupplyError): check_reference(payload,None)
        with pytest.raises(LocalSupplyError): check(payload,None,node=node)
        with pytest.raises(LocalSupplyError): check({**payload,'stage':'near_transfer'},None,node=node,connection=c,child_id=CHILD)
    published = publish_first_open(db,spec,scope)
    from home_study_agent.curriculum_first_source import quarantine_after_restore
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c,'synthetic-first-open-restore')>=1
        c.commit()
    service = CurriculumFirstSourceService(Database(db.path))
    cid = published['candidate_id']
    assert service.get(CHILD,cid)['state']=='blocked' and not service.get(CHILD,cid)['eligible']
    # Exact prior commands never restore the old authority.
    assert not service.transition(CHILD,cid,'publish',actor='parent',action_id='first-open-publish',expected_version=3)['eligible']
    service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='fresh-v',expected_version=5)
    service.transition(CHILD,cid,'review',actor='parent',action_id='fresh-r',expected_version=6,
        values=dict(decision='approve',note='Rechecked original source after restore'))
    restored = service.transition(CHILD,cid,'publish',actor='parent',action_id='fresh-p',expected_version=7)
    assert restored['eligible'] and restored['diagnostic_item_id']==published['diagnostic_item_id']


def test_first_open_transaction_rollback_concurrent_replay_and_versions(tmp_path):
    db,_,spec,_,scope = first_open_case(tmp_path)
    service = CurriculumFirstSourceService(db)
    draft = service.generate(CHILD,spec,action_id='d',expected_scope_version=scope)
    cid = draft['candidate_id']
    with pytest.raises(LocalSupplyError,match='VERSION_CONFLICT'):
        service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='wrong-version',expected_version=0)
    service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='v',expected_version=1)
    with pytest.raises(LocalSupplyError,match='BASELINE_ONLY'):
        service.transition(CHILD,cid,'review',actor='parent',action_id='bad-stage',expected_version=2,
            values=dict(decision='approve',note='No migration claim',stage_review={'stage':'near_transfer'}))
    service.transition(CHILD,cid,'review',actor='parent',action_id='r',expected_version=2,
        values=dict(decision='approve',note='Original reference contract checked'))
    with db.connect() as c:
        c.execute("CREATE TRIGGER interrupt_first_open BEFORE INSERT ON curriculum_first_source_event WHEN NEW.kind='publish' BEGIN SELECT RAISE(ABORT,'injected interruption'); END")
        c.commit()
    def publish():
        return CurriculumFirstSourceService(Database(db.path)).transition(CHILD,cid,'publish',actor='parent',action_id='p',expected_version=3)
    with pytest.raises(sqlite3.IntegrityError,match='injected'): publish()
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()
        assert c.execute('SELECT state,version FROM curriculum_first_source WHERE candidate_id=?',(cid,)).fetchone()[:]==('reviewed',3)
        c.execute('DROP TRIGGER interrupt_first_open')
        c.commit()
    with ThreadPoolExecutor(max_workers=2) as pool: a,b = list(pool.map(lambda _:publish(),range(2)))
    assert a==b and a['eligible']
    service.transition(CHILD,cid,'withdraw',actor='parent',action_id='w',expected_version=4,values=dict(note='Retract original'))
    assert not publish()['eligible']
    with db.connect() as c, pytest.raises(LocalSupplyError): validate_item(c,CHILD,a['diagnostic_item_id'])


def test_first_open_original_written_response_survives_restart_without_false_score(tmp_path):
    db,_,spec,_,scope = first_open_case(tmp_path)
    published = publish_first_open(db,spec,scope)
    sid = StudyRepository(db).start_session(CHILD,'first-open-session')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid,track='school',preferred_item_id=published['diagnostic_item_id'],client_action_id='first-open-prompt')['prompt']
    answer = '我使用题给依据逐步推理，说明该结论只适用于这些条件。'
    original = WritingOriginalService(db).capture_native_response(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],text=answer,client_action_id='first-open-original')
    result = prompts.answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=answer,
        writing_original_artifact_id=original['artifact_id'],client_action_id='first-open-answer')
    assert result['attempt']['correctness'] is None and not result['attempt']['mastery_update_allowed']
    restarted = DiagnosticPromptService(Database(db.path)).answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=answer,
        writing_original_artifact_id=original['artifact_id'],client_action_id='first-open-answer')
    assert restarted['attempt']['attempt_id']==result['attempt']['attempt_id']


def test_material_first_open_is_consumable_and_revocable_after_restart(tmp_path):
    from home_study_agent.diagnostic_prompt import _version
    from home_study_agent.open_response_materials import resolve_open_response_materials
    db,_,spec,source,scope = first_open_case(tmp_path,subject='物理',materials=True)
    published = publish_first_open(db,spec,scope)
    item_id = published['diagnostic_item_id']
    with Database(db.path).connect() as c:
        item = validate_item(c,CHILD,item_id)
        version = _version(DiagnosticPromptService._item(c,CHILD,item_id))
        view = resolve_open_response_materials(c,child_id=CHILD,diagnostic_item_id=item_id,expected_item_version=version)
        assert view['status']=='ready' and view['stimulus_text']==item['stimulus_text']
        assert view['materials'][0]['binding']==source['material_bindings'][0]
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?",(source['material_bindings'][0]['source_id'],))
        c.commit()
    with db.connect() as c:
        revoked = resolve_open_response_materials(c,child_id=CHILD,diagnostic_item_id=item_id,expected_item_version=version)
        assert revoked['status']=='blocked' and not revoked['materials']


def test_first_open_spoken_runtime_original_feedback_and_app_restart(tmp_path,monkeypatch):
    import test_open_response_spoken_runtime_http as journey
    def source_first(path,subject):
        db,_,spec,source,scope = first_open_case(path,subject=subject,mode='spoken_explanation')
        return db,source,publish_first_open(db,spec,scope)
    monkeypatch.setattr(journey,'spoken_source',source_first)
    journey.test_natural_spoken_process_asr_bound_answer_formative_and_restart(
        tmp_path,monkeypatch,'数学',None,False,None)


@pytest.mark.parametrize('materials',[False,True])
def test_first_open_cloud_question_permission_is_explicit_revocable_and_restored(tmp_path,materials):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService
    from test_writing_question_authorization import LICENSE
    db,_,spec,_,scope = first_open_case(tmp_path,materials=materials)
    published = publish_first_open(db,spec,scope)
    privacy = PrivacyLifecycleService(db,db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    service = WritingQuestionAuthorizationService(db)
    service.initialize()
    initial = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    assert initial['status']=='authorization_required',initial
    args = {k:initial[k] for k in ('diagnostic_item_id','item_version','source_version','curriculum_version')}
    if materials:
        args['material_license_bases'] = [{k:m[k] for k in ('material_key','source_version','text_hash')}
            | {'license_basis':LICENSE} for m in initial['preview']['materials']]
    grant = service.grant(CHILD,**args,license_basis=LICENSE,expected_revision=0,
        client_action_id='first-open-grant',actor_id='authenticated-parent')
    assert grant['current']['status']=='authorized'
    restored = WritingQuestionAuthorizationService(Database(db.path))
    assert restored.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])['status']=='authorized'
    CurriculumFirstSourceService(db).transition(CHILD,published['candidate_id'],'withdraw',actor='parent',
        action_id='first-open-retract',expected_version=4,values=dict(note='Retract source'))
    assert restored.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])['status']=='invalidated'


def test_first_open_review_can_schedule_true_distinct_near_training_and_retract_root(tmp_path,monkeypatch):
    from home_study_agent.config import Settings
    from test_curriculum_first_source_http import app_client,auth
    from test_school_open_reviews import school_stage,answer_and_review
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    monkeypatch.setattr('test_pet_media_evidence.CHILD',CHILD)
    monkeypatch.setattr('test_writing_stage_reviews.CHILD',CHILD)
    db,source,spec,_,scope = first_open_case(tmp_path,variants=True)
    baseline = publish_first_open(db,spec,scope)
    supply = CurriculumSupplyService(db)
    with db.connect() as c:
        excerpts = [r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence',(source['source_id'],))]
    seeded = {k:v for k,v in spec.items() if k!='knowledge_node_id'}
    from home_study_agent.open_response_source import VALIDATOR as OPEN_VALIDATOR
    seeded.update(seed_item_id=baseline['diagnostic_item_id'],validator=OPEN_VALIDATOR)
    near = school_stage(supply,{**seeded,'excerpt_id':excerpts[1]},'near_transfer')
    far = school_stage(supply,{**seeded,'excerpt_id':excerpts[2]},'far_transfer')
    with app_client(tmp_path,db) as client:
        auth(client)
        sid = StudyRepository(db).start_session(CHILD,'first-open-retest')['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
            c.commit()
        case = (db,client,dict(client.headers),sid,baseline,near,far,supply,Settings(data_dir=tmp_path,database_path=db.path))
        _,attempt,_,_,_ = answer_and_review(case,key='first-source-reviewed')
    with Database(db.path).connect() as c:
        task = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',(attempt['attempt']['evidence_id'],)).fetchone()
        assert task and task['diagnostic_item_id']==near['diagnostic_item_id'] and task['interval_label']=='school_open_near'
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',(attempt['attempt']['attempt_id'],)).fetchone()[0] is None
    CurriculumFirstSourceService(db).transition(CHILD,baseline['candidate_id'],'withdraw',actor='parent',
        action_id='training-root-retract',expected_version=4,values=dict(note='Withdraw original root'))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        reconcile_school_reviews(c,CHILD)
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]=='cancelled'
        with pytest.raises(LocalSupplyError): validate_item(c,CHILD,near['diagnostic_item_id'])
        c.commit()


def test_managed_visual_first_open_keeps_image_binding_after_restart_and_retract(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.diagnostic_prompt import _version
    from home_study_agent.open_response_materials import resolve_open_response_materials
    from test_visual_material_host import setup_case,headers,REGION
    from test_visual_curriculum_supply_http import supply_case
    from test_pet_reviewed_source_actual_http import NoCloud
    db,settings,prepared_image,args,region = setup_case(tmp_path)
    child,base = 'child','/api/parent/children/child/curriculum-supply'
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42188)) as client:
        h = headers(client)
        client.app.state.visual_materials.stage_prepared(child,'page','figure',
            revision_id=args['revision_id'],region_version=args['region_version'],prepared=prepared_image)
        stored = client.post(REGION+'/storage',json={**args,'client_action_id':'store','purpose':'local_visual_material_review_v1'},headers=h)
        assert stored.status_code==200,stored.text
        image = client.get(REGION+'/image',params={**args,'sha256':region['safe_png_sha256']},headers=h)
        assert image.status_code==200,image.text
        displayed = client.post(REGION+'/displayed',json={**args,'client_action_id':'display',
            'display_token':image.headers['X-HSC-Visual-Display-Token'],'sha256':region['safe_png_sha256'],
            'width':prepared_image.width,'height':prepared_image.height},headers=h)
        confirmed = client.post(REGION+'/confirm',json={**args,'client_action_id':'confirm',
            'display_receipt_id':displayed.json()['display_receipt_id']},headers=h)
        assert confirmed.status_code==200,confirmed.text
        binding = confirmed.json()['detail']['binding']
        source,_,seeded = supply_case(db,binding)
        with db.connect() as c:
            c.execute('''INSERT INTO knowledge_node SELECT ?,'skill',track,subject,grade_band,
                'SYNTHETIC.FIRST.VISUAL',title,description,source_ref,knowledge_version,status
                FROM knowledge_node WHERE knowledge_node_id=?''',(NODE,source['knowledge_node_id']))
            c.commit()
        source['knowledge_node_id'] = NODE
        imported = KnowledgeBaseService(db).import_source(child,dict(source_type='school_material',subject='物理',grade_band='初三',
            title='Synthetic source-first visual task',version_label='visual-test',source_ref='isolated:first-visual',
            license_status='family_owned_index',parent_reviewed=True,content_text=json.dumps(source)))
        with db.connect() as c:
            excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(imported['source_id'],)).fetchone()[0]
            assert not c.execute('SELECT 1 FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()
        CurriculumSupplyService(db).select_curriculum(child,dict(grade='初三',subject='物理',edition='visual-test',semester='上学期',
            standard_scope='第四学段',textbook_source_id=imported['source_id']),actor='parent',action_id='first-visual-scope',expected_version=1)
        spec = {k:v for k,v in seeded.items() if k not in {'seed_item_id','stage','validator'}}
        spec.update(knowledge_node_id=NODE,excerpt_id=excerpt)
        response = client.post(base+'/first-sources',json=dict(action_id='first-visual-draft',expected_version=0,
            expected_scope_version=2,source=spec),headers=h)
        assert response.status_code==200,response.text
        cid = response.json()['candidate_id']
        for version,operation in enumerate(('validate','review','publish'),1):
            command = dict(action_id='first-visual-'+operation,expected_version=version)
            if operation=='review': command.update(decision='approve',note='Checked exact managed figure and original source')
            response = client.post(base+f'/{cid}/{operation}',json=command,headers=h)
            assert response.status_code==200,response.text
            assert response.json()['state']=={'validate':'validated','review':'reviewed','publish':'published'}[operation]
        item_id = response.json()['diagnostic_item_id']
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42189)) as client:
        h = headers(client)
        assert client.get(base+f'/{cid}',headers=h).json()['eligible']
        with client.app.state.repository.database.connect() as c:
            item = validate_item(c,child,item_id)
            version = _version(DiagnosticPromptService._item(c,child,item_id))
            view = resolve_open_response_materials(c,child_id=child,diagnostic_item_id=item_id,expected_item_version=version)
            assert view['status']=='ready' and view['visual_materials'][0]['binding']==binding
            assert not view['cloud_transmission_allowed']
        assert client.post(REGION+'/retract',json={**args,'client_action_id':'first-visual-retract'},headers=h).status_code==200
        assert not client.get(base+f'/{cid}',headers=h).json()['eligible']
        with db.connect() as c,pytest.raises(LocalSupplyError): validate_item(c,child,item_id)

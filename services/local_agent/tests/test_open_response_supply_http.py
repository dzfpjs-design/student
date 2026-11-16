"""Real parent supply router and SQLite, not learner scoring or cloud teaching."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import validate_item, LocalSupplyError, score_local_item
from home_study_agent.open_response_source import SCHEMA, VALIDATOR
from home_study_agent.repository import StudyRepository
from test_pet_reviewed_source_actual_http import NoCloud

CHILD = 'child-junior-3'


def item_rows(c):
    return [tuple(row) for row in c.execute('SELECT * FROM diagnostic_item ORDER BY diagnostic_item_id')]


def prepared(tmp_path,subject,*,context='self_contained',license_status='family_owned_index'):
    db = Database(tmp_path/'open-supply.db')
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    math = subject=='数学'
    source = dict(schema=SCHEMA,subject=subject,grade='初三',
        knowledge_node_id='node-j3-math-discriminant' if math else 'node-j3-physics-experiment-reasoning',
        answer_type='open_response',assessment_mode='open_response_rubric',
        task_family='mathematical_process' if math else 'scientific_explanation',response_mode='written',
        prompt='不直接求根，分析 x²+2x+5=0 在实数范围内的解的情况，并写出判别依据和推理过程。' if math else
            '两个导体长度不同，横截面积与材料相同。提出检验长度影响电阻的方案，并说明控制变量与结论边界。',
        task_points=['说明题中条件和所用关系','用明确的过程支持结论，并说明其适用边界'],context_requirement=context)
    imported = KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',title='Isolated authored open task',
        subject=subject,grade_band='初三',version_label='isolated-open-2026',source_ref='isolated-authored-open-source',
        license_status=license_status,parent_reviewed=True,content_text=json.dumps(source,ensure_ascii=False)))
    quote = '分析任务中的条件并用可追溯的推理过程解释结论及其适用边界。'
    standard_root = tmp_path.resolve(strict=True) / 'open-standards'
    standard_root.mkdir()
    (standard_root / 'manifest.json').write_text(json.dumps({
        'format': 'hsc_official_curriculum_sources_v1',
        'sources': [{'source_key': 'isolated-open', 'pdf_filename': None, 'pages_filename': None}],
    }), encoding='utf-8')
    with db.connect() as c:
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(imported['source_id'],)).fetchone()[0]
        c.execute('''INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,subject_title,title,
            version_label,official_url,local_root_path,status,imported_at,updated_at)
            VALUES('open-standard','isolated-open','CN','national_standard',?,'Isolated standard','2022',
            'isolated:standard',?,'structured','2026-09-01','2026-09-01')''',(subject,str(standard_root)))
        c.execute("INSERT INTO curriculum_standard_page VALUES('open-standard',12,?,'fixture-hash',20)",(quote,))
        c.execute('''INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,
            requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('open-requirement','open-standard',?,'第四学段','content_requirement',?,?,'Traceable reasoning',12,12,.9,
            'verified','2026-09-01','2026-09-01')''',(subject,quote,quote))
        c.commit()
    supply = CurriculumSupplyService(db)
    supply.select_curriculum(CHILD,dict(grade='初三',subject=subject,edition='isolated-open-2026',semester='上学期',
        standard_scope='第四学段',textbook_source_id=imported['source_id']),actor='parent',action_id='open-scope',expected_version=0)
    spec = dict(seed_item_id='diag-j3-discriminant-1' if math else 'diag-j3-physics-experiment-1',stage='parallel',
        excerpt_id=excerpt,requirement_id='open-requirement',standard_page=12,standard_quote=quote,validator=VALIDATOR,
        explanation='仅审核原始任务和参考标准，不能按关键词或字数认证掌握。',difficulty=2)
    return db,supply,spec,source,imported['source_id']


@pytest.mark.parametrize('subject',['数学','物理'])
def test_real_parent_school_open_supply_publish_withdraw_and_restart(tmp_path,subject):
    db,_,spec,source,source_id = prepared(tmp_path,subject)
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,session_token='open-supply-local-session')
    base = '/api/parent/children/'+CHILD+'/curriculum-supply'
    local = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',41220),headers=local) as client:
        token = client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        assert client.get(base+f'/sources/{source_id}/options').status_code==401
        client.headers['X-HSC-Parent']=token
        options = client.get(base+f'/sources/{source_id}/options')
        assert options.status_code==200,options.text
        assert any(row['seed_item_id']==spec['seed_item_id'] and row['validator']==VALIDATOR for row in options.json()['seeds'])
        body = dict(action_id='open-draft',expected_version=0,expected_scope_version=1,source=spec)
        drafted = client.post(base+'/generate',json=body)
        assert drafted.status_code==200,drafted.text
        draft = drafted.json()
        assert draft['state']=='draft' and draft['payload']['open_response']==source
        cid = draft['candidate_id']
        assert client.post(base+'/generate',json=body).json()['candidate_id']==cid
        duplicate = client.post(base+'/generate',json={**body,'action_id':'duplicate-question'})
        assert duplicate.status_code==422 and 'CURRICULUM_DUPLICATE_QUESTION' in duplicate.text
        other = base.replace(CHILD,'child-primary-4')
        assert client.get(other+f'/{cid}').status_code==404
        assert client.get(other+f'/sources/{source_id}/options').status_code==404
        assert client.post(other+'/generate',json=body).status_code in {404,409}
        for version,operation in enumerate(('validate','review','publish'),1):
            payload = dict(action_id='open-'+operation,expected_version=version)
            if operation=='review': payload.update(decision='approve',note='Checked original task, source and profile; not independent answer recomputation')
            result = client.post(base+f'/{cid}/{operation}',json=payload)
            assert result.status_code==200,result.text
        published = result.json()
        assert published['state']=='published' and published['eligible']
        assert published['review_packet']['open_response']==source
        assert published['local_only'] and not published['cloud_transmission_allowed']
        with db.connect() as c:
            item = validate_item(c,CHILD,published['diagnostic_item_id'])
            assert item['answer_type']=='open_response' and item['assessment_mode']=='open_response_rubric'
            assert not {'exact_answers','keywords','local_rule'} & json.loads(item['expected_answer_json']).keys()
            assert score_local_item(item,'yes') is None
            assert score_local_item(item,' '.join(source['task_points'])) is None
            proof = json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',(cid,)).fetchone()[0])
            assert proof['open_response_node']['knowledge_node_id']==source['knowledge_node_id'] and 'writing_node' not in proof
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0]==0
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',41221),headers=local) as client:
        client.headers['X-HSC-Parent']=client.post('/v1/parent/unlock',json={'pin':'7391'}).json()['parent_token']
        reopened = client.get(base+f'/{cid}')
        assert reopened.status_code==200 and reopened.json()['eligible'],reopened.text
        revoked = client.post(base+f'/{cid}/withdraw',json={'action_id':'open-withdraw','expected_version':4,'note':'Source withdrawn'})
        assert revoked.status_code==200 and not revoked.json()['eligible'],revoked.text
        with db.connect() as c,pytest.raises(LocalSupplyError): validate_item(c,CHILD,published['diagnostic_item_id'])


def publish_open(service,spec):
    draft = service.generate_draft(CHILD,spec,author='author',action_id='draft')
    cid = draft['candidate_id']
    checked = service.validate(CHILD,cid,actor='validator',action_id='validate',expected_version=1)
    assert checked['state']=='validated',checked['blockers']
    reviewed = service.review(CHILD,cid,decision='approve',actor='parent',note='Checked source and reference criteria',
        action_id='review',expected_version=2)
    assert reviewed['state']=='reviewed',reviewed['blockers']
    return service.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)


@pytest.mark.parametrize('fault',['mode','type','points','node','source_text','source_withdrawal','standard','seed','selection'])
def test_published_open_revalidates_exact_source_and_mode(tmp_path,fault):
    db,service,spec,source,source_id = prepared(tmp_path,'数学',license_status='public')
    result = publish_open(service,spec)
    item_id = result['diagnostic_item_id']
    assert result['eligible']
    with db.connect() as c:
        if fault=='mode': c.execute("UPDATE diagnostic_item SET assessment_mode='objective' WHERE diagnostic_item_id=?",(item_id,))
        elif fault=='type': c.execute("UPDATE diagnostic_item SET answer_type='text' WHERE diagnostic_item_id=?",(item_id,))
        elif fault=='points': c.execute("UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?",(json.dumps({'local_rule':'local_exact_v1','exact_answers':['yes']}),item_id))
        elif fault=='node': c.execute("UPDATE knowledge_node SET subject='物理' WHERE knowledge_node_id=?",(source['knowledge_node_id'],))
        elif fault=='source_text': c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?',(json.dumps({**source,'prompt':'Changed source question'}),spec['excerpt_id']))
        elif fault=='source_withdrawal': c.execute("UPDATE knowledge_source SET review_status='pending' WHERE source_id=?",(source_id,))
        elif fault=='standard': c.execute("UPDATE curriculum_standard_requirement SET maturity='retired' WHERE requirement_id=?",(spec['requirement_id'],))
        elif fault=='seed': c.execute("UPDATE diagnostic_item SET prompt='Changed seed' WHERE diagnostic_item_id=?",(spec['seed_item_id'],))
        else: c.execute("UPDATE curriculum_supply_selection SET version=version+1 WHERE child_id=?",(CHILD,))
        c.commit()
        with pytest.raises(LocalSupplyError): validate_item(c,CHILD,item_id)
    reopened = CurriculumSupplyService(Database(db.path)).get(CHILD,result['candidate_id'])
    assert not reopened['eligible'] and reopened['blockers']


@pytest.mark.parametrize('fault',['material','wrong_seed','same_seed_question','keyword_scoring','unknown_schema'])
def test_missing_material_wrong_seed_or_unreviewable_contract_cannot_publish(tmp_path,fault):
    db,service,spec,source,_ = prepared(tmp_path,'数学',context='external_material' if fault=='material' else 'self_contained')
    with db.connect() as c:
        before_items = item_rows(c)
        if fault=='wrong_seed': spec['seed_item_id']='diag-j3-physics-experiment-1'
        if fault=='same_seed_question':
            source['prompt']=c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',(spec['seed_item_id'],)).fetchone()[0]
        if fault=='keyword_scoring': source['exact_answers']=['yes']
        if fault=='unknown_schema': source['schema']='guessed_open_v1'
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?',(json.dumps(source),spec['excerpt_id']))
        c.commit()
    try:
        draft = service.generate_draft(CHILD,spec,author='author',action_id='bad')
    except LocalSupplyError:
        pass
    else:
        assert draft['state']=='blocked' and draft['blockers']
        checked = service.validate(CHILD,draft['candidate_id'],actor='validator',action_id='check',expected_version=1)
        assert checked['state']=='blocked'
        if fault=='material': assert 'OPEN_RESPONSE_MATERIAL_BINDING_REQUIRED' in checked['blockers']
    with db.connect() as c:
        assert item_rows(c) == before_items
        assert not c.execute('SELECT 1 FROM model_call').fetchone()


def test_same_grade_child_cannot_borrow_published_open_item(tmp_path):
    db,service,spec,_,_ = prepared(tmp_path,'数学')
    published = publish_open(service,spec)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='初三' WHERE child_id='child-primary-4'")
        c.commit()
        with pytest.raises(LocalSupplyError): validate_item(c,'child-primary-4',published['diagnostic_item_id'])
    with pytest.raises(KeyError): service.get('child-primary-4',published['candidate_id'])
    assert service.get(CHILD,published['candidate_id'])['eligible']


@pytest.mark.parametrize('stage',['near_transfer','far_transfer'])
def test_open_transfer_requires_exact_explicit_stage_review(tmp_path,stage):
    _,service,spec,_,_ = prepared(tmp_path,'数学')
    spec['stage']=stage
    draft = service.generate_draft(CHILD,spec,author='author',action_id='draft')
    cid = draft['candidate_id']
    checked = service.validate(CHILD,cid,actor='validator',action_id='validate',expected_version=1)
    context = checked['stage_review_context']
    args = dict(decision='approve',actor='parent',note='Explicit source review',action_id='review',expected_version=2)
    with pytest.raises(LocalSupplyError,match='CURRICULUM_OPEN_STAGE_REVIEW_REQUIRED'):
        service.review(CHILD,cid,**args)
    receipt = dict(schema=context['schema'],stage=stage,binding_version=context['binding_version'],
        source_quote=context['source_prompt'][:8],candidate_quote=context['candidate_prompt'][:12],
        same_skill_rationale='Both require reasoning from the discriminant and actual equation conditions.',
        changed_context_rationale='New task asks for justification without computing roots, rather than one numeric answer.')
    reviewed = service.review(CHILD,cid,stage_review=receipt,**args)
    assert reviewed['state']=='reviewed',reviewed['blockers']
    published = service.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)
    assert published['eligible']
    check = published['events'][1]['result']['check']
    assert not check['mastery_claim_allowed'] and not check['stage_qualification_allowed']


def test_publication_inserts_correct_mode_and_rolls_back_atomically(tmp_path):
    db,service,spec,_,_ = prepared(tmp_path,'数学')
    cid = service.generate_draft(CHILD,spec,author='author',action_id='draft')['candidate_id']
    service.validate(CHILD,cid,actor='validator',action_id='validate',expected_version=1)
    service.review(CHILD,cid,actor='parent',decision='approve',note='Source reviewed',action_id='review',expected_version=2)
    with db.connect() as c:
        before_items = item_rows(c)
        c.executescript('''CREATE TRIGGER verify_open_insert BEFORE INSERT ON diagnostic_item
            WHEN NEW.source_ref LIKE 'parent_approved_specialized:%'
              AND (NEW.answer_type!='open_response' OR NEW.assessment_mode!='open_response_rubric')
            BEGIN SELECT RAISE(ABORT,'WRONG_INITIAL_MODE'); END;
            CREATE TRIGGER interrupted_open_insert AFTER INSERT ON diagnostic_item
            WHEN NEW.answer_type='open_response' BEGIN SELECT RAISE(ABORT,'INTERRUPTED_PUBLICATION'); END;''')
    with pytest.raises(sqlite3.IntegrityError,match='INTERRUPTED_PUBLICATION'):
        service.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)
    with db.connect() as c:
        assert c.execute('SELECT state,version FROM curriculum_supply_package WHERE candidate_id=?',(cid,)).fetchone()[:]==('reviewed',3)
        assert not c.execute("SELECT 1 FROM curriculum_supply_event WHERE kind='publish'").fetchone()
        assert item_rows(c) == before_items
        c.execute('DROP TRIGGER interrupted_open_insert')
        c.commit()
    restarted = CurriculumSupplyService(Database(db.path))
    published = restarted.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)
    assert published['eligible']
    assert restarted.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)['diagnostic_item_id']==published['diagnostic_item_id']
    with db.connect() as c:
        after = item_rows(c)
        assert len(after) == len(before_items) + 1
        assert [row for row in after if row[0] != published['diagnostic_item_id']] == before_items


def test_actual_http_missing_material_and_strict_source_contract(tmp_path):
    db,_,spec,_,source_id = prepared(tmp_path,'物理',context='external_material')
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,session_token='open-material-session')
    base = '/api/parent/children/'+CHILD+'/curriculum-supply'
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',41222),
                    headers={'X-HSC-Session':settings.session_token}) as client:
        client.headers['X-HSC-Parent']=client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
        with db.connect() as c:
            before_items = item_rows(c)
        options = client.get(base+f'/sources/{source_id}/options')
        assert options.status_code==200,options.text
        assert not any(seed['validator']==VALIDATOR for seed in options.json()['seeds'])
        body = dict(action_id='missing-material',expected_version=0,expected_scope_version=1,source=spec)
        for bad in ({**body,'actor':'parent'}, {**body,'source':{**spec,'exact_answers':['yes']}},
                    {**body,'expected_scope_version':'1'}):
            assert client.post(base+'/generate',json=bad).status_code==422
        drafted = client.post(base+'/generate',json=body)
        assert drafted.status_code==200,drafted.text
        package = drafted.json()
        assert package['state']=='blocked' and 'OPEN_RESPONSE_MATERIAL_BINDING_REQUIRED' in package['blockers']
        cid = package['candidate_id']
        validated = client.post(base+f'/{cid}/validate',json={'action_id':'validate','expected_version':1})
        assert validated.status_code==200,validated.text
        assert validated.json()['state']=='blocked' and not validated.json()['eligible']
        with db.connect() as c:
            assert item_rows(c) == before_items
            assert not c.execute('SELECT 1 FROM model_call').fetchone()

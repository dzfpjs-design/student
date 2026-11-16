"""Real candidate import/publication authority on isolated synthetic originals."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService,InvalidKnowledgeSourceError
from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.pet_reviewed_source import parse_listening_source,published_listening_source
from home_study_agent.pet_listening_requirement import listening_requirement,listening_unit_requirement
from test_pet_reviewed_source_publication import prepared
from test_pet_reviewed_source_contract import packet
from test_school_listening_modality import prepared_school,school_packet
from test_pet_reviewed_source_actual_http import NoCloud


def bundle(track):
    rows=[]
    for unit in ('a','b'):
        members=[unit+'-one',unit+'-two']
        for key in members:
            base=school_packet() if track=='school' else packet()
            rows.append({**base,'schema':track+'_listening_source_v2','question_key':key,
                'prompt':'Which place is requested in original '+key+'?',
                'stimulus_text':'An authored original message for unit '+unit+'. Wait at the red bridge.',
                'instruction':'Listen '+('once' if unit=='a' else 'twice')+' and answer this question group.',
                'required_play_count':1 if unit=='a' else 2,
                'delivery_unit':dict(unit_key=unit,member_keys=members,original_audio_key='original-'+unit,
                    required_play_count=1 if unit=='a' else 2)})
    return rows


def setup(root,monkeypatch,track):
    rows=bundle(track)
    original=KnowledgeBaseService.import_source
    def import_bundle(self,child,payload):
        return original(self,child,{**payload,'content_text':'\n'.join(json.dumps(p,ensure_ascii=False) for p in rows)})
    with monkeypatch.context() as scoped:
        scoped.setattr(KnowledgeBaseService,'import_source',import_bundle)
        data=prepared_school(root,rows[0]) if track=='school' else prepared(root,rows[0])
    db,supply,spec=data[:3]
    child='child-junior-3' if track=='school' else 'child-primary-4'
    with db.connect() as c:
        excerpt_ids=[r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt ORDER BY sequence')]
    return db,supply,spec,child,rows,excerpt_ids


def publish(client,headers,spec,child,key):
    base='/api/parent/children/'+child+'/curriculum-supply'
    response=client.post(base+'/generate',headers=headers,json=dict(source=spec,action_id=key+'-draft',
        expected_version=0,expected_scope_version=1))
    assert response.status_code==200,response.text
    cid=response.json()['candidate_id']
    for version,operation in enumerate(('validate','review','publish'),1):
        body=dict(action_id=key+'-'+operation,expected_version=version)
        if operation=='review':body.update(decision='approve',note='核对完整原题组、原音键及原播放要求；不是学习成绩。')
        response=client.post(base+'/'+cid+'/'+operation,headers=headers,json=body)
        assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('track',['pet','school'])
def test_real_http_multi_unit_source_publish_restart_withdraw_and_owner_scope(tmp_path,monkeypatch,track):
    db,supply,spec,child,rows,excerpts=setup(tmp_path,monkeypatch,track)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,data_encryption_key=b'7'*32)
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42421)) as client:
        headers={'X-HSC-Parent':client.post('/v1/parent/setup',json={'pin':'9372'}).json()['parent_token']}
        published=[publish(client,headers,{**spec,'excerpt_id':eid},child,'unit-'+str(i)) for i,eid in enumerate(excerpts)]
        with db.connect() as c:
            actual=[published_listening_source(c,child,r['diagnostic_item_id']) for r in published]
            assert actual==rows
            requirements=[listening_requirement(c,child,r['diagnostic_item_id']) for r in published]
            assert requirements[0]==requirements[1] and requirements[2]==requirements[3]
            assert requirements[0]['version']!=requirements[2]['version']
            assert requirements[0]['member_keys']==['a-one','a-two']
            assert requirements[2]['required_play_count']==2
            foreign='child-primary-4' if track=='school' else 'child-junior-3'
            assert listening_requirement(c,foreign,published[0]['diagnostic_item_id'])['status']=='unknown'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        with Database(db.path).connect() as c:
            assert listening_unit_requirement(c,child,published[0]['diagnostic_item_id'])==requirements[0]
        base='/api/parent/children/'+child+'/curriculum-supply'
        revoked=client.post(base+'/'+published[1]['candidate_id']+'/withdraw',headers=headers,
            json=dict(action_id='withdraw-unit-member',expected_version=4,note='撤回此原题审核，不覆盖历史。'))
        assert revoked.status_code==200,revoked.text
        with db.connect() as c:
            assert listening_requirement(c,child,published[1]['diagnostic_item_id'])['status']=='unknown'


@pytest.mark.parametrize('fault',['missing_member','duplicate_question','conflicting_script','conflicting_count',
    'conflicting_audio','foreign_scope','request_positions','unknown_field'])
def test_original_group_must_be_complete_and_consistent_before_import(tmp_path,monkeypatch,fault):
    rows=bundle('school')
    if fault=='missing_member':rows.pop(1)
    elif fault=='duplicate_question':rows[1]['question_key']=rows[0]['question_key']
    elif fault=='conflicting_script':rows[1]['stimulus_text']='Different original audio.'
    elif fault=='conflicting_count':
        rows[1]['required_play_count']=2;rows[1]['delivery_unit']={**rows[1]['delivery_unit'],'required_play_count':2}
    elif fault=='conflicting_audio':rows[1]['delivery_unit']={**rows[1]['delivery_unit'],'original_audio_key':'another-audio'}
    elif fault=='foreign_scope':rows[1]['knowledge_node_id']='different-node'
    elif fault=='request_positions':rows[1]['delivery_unit']={**rows[1]['delivery_unit'],'positions':[1,2]}
    else:rows[1]['delivery_unit']={**rows[1]['delivery_unit'],'source_reviewed':True}
    db=Database(tmp_path/'rejected.sqlite');db.initialize()
    from home_study_agent.repository import StudyRepository
    StudyRepository(db).seed_family()
    with pytest.raises(InvalidKnowledgeSourceError):
        KnowledgeBaseService(db).import_source('child-junior-3',dict(source_type='school_material',title='Rejected original group',
            subject='英语',grade_band='初三',version_label='isolated',source_ref='isolated:no-authority',license_status='public',
            parent_reviewed=True,content_text='\n'.join(json.dumps(s) for s in rows)))
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0]==0


@pytest.mark.parametrize('fault',['source_hash','member_script'])
def test_current_published_group_cannot_hide_changed_source_authority(tmp_path,monkeypatch,fault):
    db,supply,spec,child,rows,excerpts=setup(tmp_path,monkeypatch,'school')
    from test_school_listening_modality import publish_school
    published=publish_school(supply,spec)
    with db.connect() as c:
        before=listening_requirement(c,child,published['diagnostic_item_id'])
        assert before['status']=='known'
        if fault=='source_hash':c.execute("UPDATE knowledge_source SET content_hash='changed-original'")
        else:
            changed={**rows[1],'stimulus_text':'A replaced shared original recording.'}
            c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?',(json.dumps(changed),excerpts[1]))
        c.commit()
    with Database(db.path).connect() as c:
        assert listening_requirement(c,child,published['diagnostic_item_id'])['status']=='unknown'


def test_legacy_v1_single_question_remains_original_and_does_not_claim_a_group(tmp_path):
    db,supply,spec,original=prepared(tmp_path)
    from test_pet_reviewed_source_publication import publish as existing_publish
    published=existing_publish(supply,spec)
    with db.connect() as c:
        assert published_listening_source(c,'child-primary-4',published['diagnostic_item_id'])==original
        requirement=listening_requirement(c,'child-primary-4',published['diagnostic_item_id'])
        assert requirement['version'].startswith('pet_reviewed_listening_instruction_v1:')
        unit=listening_unit_requirement(c,'child-primary-4',published['diagnostic_item_id'])
        assert unit['member_keys']==[published['diagnostic_item_id']] and unit['version']==requirement['version']


def test_mixed_typed_source_is_explicitly_rejected_and_complete_original_publishes(tmp_path,monkeypatch):
    from home_study_agent.repository import StudyRepository
    rejected=Database(tmp_path/'rejected.sqlite');rejected.initialize();StudyRepository(rejected).seed_family()
    rows=bundle('school')
    with pytest.raises(InvalidKnowledgeSourceError,match='结构化题源不可混入缺损题目或普通索引'):
        KnowledgeBaseService(rejected).import_source('child-junior-3',dict(source_type='school_material',
            title='Mixed original',subject='英语',grade_band='初三',version_label='isolated',
            source_ref='isolated:mixed-source',license_status='public',parent_reviewed=True,
            content_text='原卷说明：按题组播放原音。\n'+'\n'.join(json.dumps(r) for r in rows)))
    with rejected.connect() as c:
        assert c.execute('SELECT count(*) FROM knowledge_source').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM knowledge_excerpt').fetchone()[0]==0
    valid_root=tmp_path/'complete';valid_root.mkdir()
    db,supply,spec,child,rows,excerpts=setup(valid_root,monkeypatch,'school')
    settings=Settings(data_dir=valid_root,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42421)) as client:
        headers={'X-HSC-Parent':client.post('/v1/parent/setup',json={'pin':'9372'}).json()['parent_token']}
        published=[publish(client,headers,{**spec,'excerpt_id':eid},child,'complete-'+str(i)) for i,eid in enumerate(excerpts)]
        with db.connect() as c:
            requirements=[listening_requirement(c,child,row['diagnostic_item_id']) for row in published]
            assert all(r['status']=='known' for r in requirements)
            assert requirements[0]==requirements[1] and requirements[2]==requirements[3]
            assert [published_listening_source(c,child,row['diagnostic_item_id']) for row in published]==rows
            c.execute("UPDATE knowledge_source SET content_hash='changed-complete-source'");c.commit()
        with db.connect() as c:
            assert listening_requirement(c,child,published[0]['diagnostic_item_id'])['status']=='unknown'

"""Primary text-material and calibration source inputs; no certificate is minted."""
import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import LocalSupplyError, validate_item, score_local_item
from home_study_agent.open_response_materials import bind_material_excerpt
from home_study_agent.open_response_profile import profile_for_item
from home_study_agent.repository import StudyRepository
from home_study_agent.rubric_calibration_evidence import source_snapshot
from home_study_agent.writing_response_origin import WritingOriginalService
from test_primary_open_response_http import CHILD, CASES, prepared


@pytest.mark.parametrize('slug', list(CASES))
def test_primary_bound_text_material_original_calibration_input_and_rollback(tmp_path, slug):
    db, specs, source, _, text = prepared(tmp_path, slug)
    material = KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',subject=source['subject'],
        grade_band='四年级',title='Synthetic primary task material',version_label='primary-text-1',
        source_ref='isolated:primary-material',license_status='public',parent_reviewed=True,
        content_text=CASES[slug][2][0]))
    with db.connect() as c:
        eid=c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(material['source_id'],)).fetchone()[0]
        binding=bind_material_excerpt(c,child_id=CHILD,excerpt_id=eid,material_key='given')
    original=dict(source,schema='reviewed_open_response_source_v2',context_requirement='external_material',
        material_semantics='text_only',material_bindings=[binding],prompt='请根据所附材料完成本题，并说明原文或条件依据。')
    imported=KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',subject=source['subject'],
        grade_band='四年级',title='Synthetic primary material task',version_label='isolated-primary-v1',
        source_ref='isolated:primary-material-question',license_status='public',parent_reviewed=True,
        content_text=json.dumps(original,ensure_ascii=False)))
    with db.connect() as c:
        excerpt=c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(imported['source_id'],)).fetchone()[0]
        scope=json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection WHERE child_id=?',(CHILD,)).fetchone()[0])
    service=CurriculumSupplyService(db)
    service.select_curriculum(CHILD,dict(scope,textbook_source_id=imported['source_id']),actor='parent',
        action_id='material-scope',expected_version=1)
    draft=service.generate_draft(CHILD,dict(specs[0],excerpt_id=excerpt),author='synthetic-author',action_id='material-draft')
    cid=draft['candidate_id']
    service.validate(CHILD,cid,actor='validator',action_id='material-check',expected_version=1)
    service.review(CHILD,cid,actor='parent',decision='approve',note='Synthetic complete task material checked',
        action_id='material-review',expected_version=2)
    item_id=service.publish(CHILD,cid,actor='parent',action_id='material-publish',expected_version=3)['diagnostic_item_id']
    with db.connect() as c:
        item=validate_item(c,CHILD,item_id)
        node=dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone())
        assert profile_for_item(item,node)['version']=='school_primary_open_response_profiles_v1'
        assert score_local_item(item,text) is None
        assert CASES[slug][2][0] in item['stimulus_text']
    sid=StudyRepository(db).start_session(CHILD,'material-session')['session_id']
    prompts=DiagnosticPromptService(db)
    p=prompts.prepare(sid,track='school',preferred_item_id=item_id,client_action_id='material-ask')['prompt']
    capture=dict(prompt_id=p['prompt_id'],item_version=p['item_version'],text=text,client_action_id='material-original')
    artifact=WritingOriginalService(db).capture_native_response(sid,**capture)
    with pytest.raises(ValueError,match='ACTION_CONFLICT'):
        WritingOriginalService(db).capture_native_response(sid,**dict(capture,text=text+' changed'))
    answer=dict(prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,
        writing_original_artifact_id=artifact['artifact_id'],client_action_id='material-answer')
    scored=prompts.answer(sid,**answer)
    evidence=scored['attempt']['evidence_id']
    with Database(db.path).connect() as c:
        before=source_snapshot(c,CHILD,evidence)
        assert before['modality']=='original_written_text' and before['grade']=='四年级'
        assert before['subject']==source['subject']
        with pytest.raises(ValueError): source_snapshot(c,'child-junior-3',evidence)
        for sql,args in (
            ("UPDATE knowledge_source SET child_id='child-junior-3' WHERE source_id=?",(material['source_id'],)),
            ("UPDATE knowledge_source SET review_status='pending' WHERE source_id=?",(material['source_id'],)),
            ("UPDATE knowledge_excerpt SET content_text='replacement' WHERE excerpt_id=?",(eid,)),
            ("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(artifact['artifact_id'],)),
        ):
            c.execute('BEGIN IMMEDIATE')
            c.execute(sql,args)
            with pytest.raises(ValueError): source_snapshot(c,CHILD,evidence)
            c.rollback()
            assert source_snapshot(c,CHILD,evidence)==before
        assert not c.execute('SELECT 1 FROM model_call').fetchone()
    assert DiagnosticPromptService(Database(db.path)).answer(sid,**answer)['attempt']['attempt_id']==scored['attempt']['attempt_id']

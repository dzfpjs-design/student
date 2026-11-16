"""Reviewed non-seed written tasks, without keyword or exact-answer grading."""
import json

import pytest

from home_study_agent.local_supply import validate_item, LocalSupplyError
from test_pet_reviewed_source_publication import prepared as source_fixture, publish, CHILD


def source_packet():
    return {'schema':'pet_writing_source_v1','modality':'writing','assessment_mode':'human_rubric',
        'subject':'英语','grade':'四年级','knowledge_node_id':'node-pet-writing-email',
        'prompt':'Write an email to Ben. Describe the school concert you attended, say which part you enjoyed, and invite him to the next concert. Write 35-55 words.',
        'task_points':['Describe the concert','Explain the part enjoyed','Invite Ben to the next concert'],
        'min_words':35,'max_words':55}


def prepared(tmp_path):
    packet = source_packet()
    db, service, spec, _ = source_fixture(tmp_path, packet)
    quote = '围绕写作任务说明活动信息、表达感受并组织连贯邀请。'
    with db.connect() as c:
        c.execute("UPDATE curriculum_standard_page SET content_text=? WHERE source_id='pet-std'", (quote,))
        c.execute("UPDATE curriculum_standard_requirement SET title='Writing task',requirement_text=?,observable_outcome='Organized written invitation' WHERE requirement_id='pet-req'", (quote,))
        c.commit()
    spec.update(seed_item_id='diag-pet-writing-1',validator='pet_writing_rubric_source_v1',
        standard_quote=quote,explanation='Check the original written response against all three task points; no unique exact answer.')
    return db, service, spec, packet


def test_nonseed_writing_source_publish_preserves_subjective_contract_and_withdrawal(tmp_path):
    from home_study_agent.writing_response_origin import writing_item
    db, service, spec, packet = prepared(tmp_path)
    published = publish(service, spec)
    from home_study_agent.curriculum_supply_http import PackageResponse
    PackageResponse.model_validate({**published,'local_only':True,'cloud_transmission_allowed':False})
    assert published['eligible'], published
    with db.connect() as c:
        item, _ = writing_item(c, CHILD, published['diagnostic_item_id'])
        assert item['diagnostic_item_id'] not in {'diag-pet-writing-1','diag-pet-writing-2'}
        assert item['answer_type'] == 'writing' and item['assessment_mode'] == 'pet_reference_rubric'
        rubric = json.loads(item['expected_answer_json'])
        assert rubric['task_points'] == packet['task_points']
        assert not {'local_rule','exact_answers','keyword_groups'} & set(rubric)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    service.withdraw(CHILD, published['candidate_id'], actor='parent', note='Source withdrawn', action_id='withdraw', expected_version=4)
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, CHILD, published['diagnostic_item_id'])


@pytest.mark.parametrize('fault', ['objective','voice','missing_points','wrong_node','forged_exact','duplicate_key'])
def test_written_source_rejects_incompatible_modality_or_unbound_rubric(tmp_path, fault):
    db, service, spec, packet = prepared(tmp_path)
    if fault == 'objective': packet['assessment_mode'] = 'objective'
    elif fault == 'voice': packet['modality'] = 'speaking'
    elif fault == 'missing_points': packet['task_points'] = []
    elif fault == 'wrong_node': packet['knowledge_node_id'] = 'node-pet-listening-detail'
    elif fault == 'forged_exact': packet['expected_answer'] = {'exact_answers':['concert']}
    content = json.dumps(packet)
    if fault == 'duplicate_key': content = content[:-1] + ',"modality":"writing"}'
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?', (content,spec['excerpt_id']))
        c.commit()
    if fault == 'wrong_node':
        draft = service.generate_draft(CHILD, spec, author='author', action_id='draft', expected_scope_version=1)
        assert draft['state'] == 'blocked'
    else:
        with pytest.raises(LocalSupplyError): service.generate_draft(CHILD, spec, author='author', action_id='draft', expected_scope_version=1)


def test_published_written_original_answer_rubric_stage_and_source_withdrawal(tmp_path):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.writing_response_origin import WritingOriginalService, bind_written_attempt
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.writing_rubric import prepare_writing_review
    from test_pet_media_evidence import local_review
    from test_specialized_media_stage import node
    db, service, spec, _ = prepared(tmp_path)
    published = publish(service, spec)
    sid = StudyRepository(db).start_session(CHILD,'start-writing')['session_id']
    prompts = DiagnosticPromptService(db)
    p = prompts.prepare(sid, track='pet', preferred_item_id=published['diagnostic_item_id'], client_action_id='ask')['prompt']
    text = 'Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance because the music was cheerful. Would you like to come with me to our next concert in June? Best wishes, Mia.'
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], text=text, client_action_id='write')
    answer = prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text, client_action_id='answer')
    assert answer['attempt']['score_status'] != 'verified_local'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_written_attempt(c, child_id=CHILD, session_id=sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
            attempt_id=answer['attempt']['attempt_id'], original_artifact_id=original['artifact_id'])
        c.commit()
    assessment = local_review((db,None,sid,p,None,None), p,text,approve=False,scored=answer)
    rubric = RubricEvidenceService(db)
    with db.connect() as c:
        binding = prepare_writing_review(c,CHILD,assessment['assessment_id'])
    rubric.review(CHILD,assessment['assessment_id'],expected_revision=0,client_action_id='review-writing',
        reviewer_id='parent',reviewer_role='parent',operation='approve',reason='Synthetic manuscript human fixture',
        independent=True,hint_level=0,review_profile='pet_writing_text_v1',writing_evidence={'binding_version':binding['binding_version'],
            'task_point_evidence':[{'task_point':v,'response_quote':text} for v in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'],score=3,response_quotes=[text],rationale='Read this original against the task') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=v,response_quote=text) for v in assessment['criteria']])
    assert node(db,'node-pet-writing-email')['qualified_stage'] == 'baseline'
    service.withdraw(CHILD,published['candidate_id'],actor='parent',note='Original task withdrawn',action_id='withdraw',expected_version=4)
    assert node(db,'node-pet-writing-email')['qualified_stage'] is None


@pytest.mark.parametrize('stage', ['near_transfer','far_transfer'])
def test_writing_transfer_form_requires_explicit_original_source_review(tmp_path, stage):
    from datetime import datetime,timezone
    from home_study_agent.specialized_stages import _item_source
    db, service, spec, _ = prepared(tmp_path)
    spec['stage'] = stage
    draft = service.generate_draft(CHILD,spec,author='local:source-author',action_id='draft',expected_scope_version=1)
    cid = draft['candidate_id']
    checked = service.validate(CHILD,cid,actor='parent',action_id='validate',expected_version=1)
    context = checked['stage_review_context']
    assert context
    review = {'schema':context['schema'],'stage':stage,'binding_version':context['binding_version'],
        'source_quote':context['source_prompt'],'candidate_quote':context['candidate_prompt'],
        'same_skill_rationale':'Explicit fixture review of the same email organization task family',
        'changed_context_rationale':'Explicit fixture review of different event and communicative setting'}
    with pytest.raises(LocalSupplyError):
        service.review(CHILD,cid,actor='parent',decision='approve',note='No stage proof',action_id='bad-review',expected_version=2)
    result = service.review(CHILD,cid,actor='parent',decision='approve',note='Checked explicit transfer form',
        stage_review=review,action_id='review',expected_version=2)
    assert result['state'] == 'reviewed'
    published = service.publish(CHILD,cid,actor='parent',action_id='publish',expected_version=3)
    with db.connect() as c:
        item = validate_item(c,CHILD,published['diagnostic_item_id'])
        source, reason = _item_source(c,CHILD,item,datetime.now(timezone.utc))
        assert reason is None and source['stage'] == stage and source['source_root_item_id'] == 'diag-pet-writing-1'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0

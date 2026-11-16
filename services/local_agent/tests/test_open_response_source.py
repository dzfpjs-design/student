"""Pure reviewed-source/profile contracts, not publication or live grading."""
import copy
import json

import pytest

from home_study_agent.open_response_profile import profile_for, profile_for_item
from home_study_agent.open_response_source import SCHEMA, VALIDATOR, parse, check, source_payload


CASES = [('数学','mathematical_process'),('语文','textual_interpretation'),('语文','communicative_writing'),
    ('英语','textual_interpretation'),('英语','communicative_writing'),('物理','scientific_explanation'),
    ('化学','scientific_explanation'),('生物','scientific_explanation'),('历史','historical_explanation'),
    ('地理','geographic_explanation'),('道德与法治','civic_argument')]


def packet(subject='物理',family='scientific_explanation',mode='written'):
    return dict(schema=SCHEMA,subject=subject,grade='初三',knowledge_node_id='synthetic-school-node',
        answer_type='open_response',assessment_mode='open_response_rubric',task_family=family,response_mode=mode,
        prompt='两次实验只改变导体长度，其余条件不变。解释为什么不能据此断言温度对电阻没有影响。',
        task_points=['区分实验改变的条件与保持不变的条件','说明现有观察可以支持和不能支持的结论'],
        context_requirement='self_contained')


def inputs(source):
    seed = {'diagnostic_item_id':'synthetic-seed','knowledge_node_id':source['knowledge_node_id'],
        'answer_type':'text','prompt':'A distinct previously reviewed source task.'}
    node = dict(knowledge_node_id=source['knowledge_node_id'],subject=source['subject'],grade_band='初三',track='school',status='active')
    payload = dict(validator=VALIDATOR,stage='parallel',prompt=source['prompt'],expected_answer=None,open_response=source)
    return payload,seed,node


@pytest.mark.parametrize('subject,family',CASES)
@pytest.mark.parametrize('mode',['written','spoken_explanation'])
def test_all_school_families_have_specific_evidence_profiles_without_exact_or_mastery_claims(subject,family,mode):
    source = packet(subject,family,mode)
    original = copy.deepcopy(source)
    parsed = parse(json.dumps(source,ensure_ascii=False))
    payload,seed,node = inputs(parsed)
    result = check(payload,seed,node=node)
    assert result['qualification']=='original_reference_checked_not_computed_or_skill_certified'
    assert result['assessment_mode']=='open_response_rubric' and result['answer_type']=='open_response'
    assert result['mastery_claim_allowed'] is False and result['cloud_transmission_allowed'] is False
    assert result['expected_answer']['task_points']==source['task_points']
    assert not {'exact_answers','keywords','keyword_groups','local_rule'} & result['expected_answer'].keys()
    descriptor = profile_for(subject,task_family=family,response_mode=mode)
    assert descriptor['dimensions'] and len({d['dimension_id'] for d in descriptor['dimensions']})==len(descriptor['dimensions'])
    assert all(d['criterion'] and d['max_score']==3 for d in descriptor['dimensions'])
    assert descriptor['automatic_certification'] is False and descriptor['score_status']=='reference'
    assert descriptor['qualification_status']=='needs_review'
    assert descriptor['human_review_required_for_formative_feedback'] is False
    assert descriptor['acoustic_assessment_allowed'] is False
    assert descriptor['original_requirement']==('written_original' if mode=='written' else 'original_answer_voice')
    assert source==original


@pytest.mark.parametrize('fault',['pet','other_node','other_subject','other_grade','retired','voice_seed','same_question',
    'extra_exact','bare_confirmed','unknown_family','external_material','wrong_expected','wrong_prompt'])
def test_source_contract_rejects_scope_laundering_and_missing_material(fault):
    source = packet()
    payload,seed,node = inputs(source)
    if fault=='pet': node['track']='pet'
    elif fault=='other_node': node['knowledge_node_id']='other-node'
    elif fault=='other_subject': node['subject']='化学'
    elif fault=='other_grade': node['grade_band']='四年级'
    elif fault=='retired': node['status']='retired'
    elif fault=='voice_seed': seed['answer_type']='speaking'
    elif fault=='same_question': seed['prompt']=source['prompt']
    elif fault=='extra_exact': source['expected_answer']={'exact_answers':['电阻']}
    elif fault=='bare_confirmed': source['cloud_allowed']=True
    elif fault=='unknown_family': source['task_family']='model_guessed_skill'
    elif fault=='external_material': source['context_requirement']='external_material'
    elif fault=='wrong_expected': payload['expected_answer']='电阻'
    elif fault=='wrong_prompt': payload['prompt']='Changed original'
    with pytest.raises(ValueError,match='OPEN_RESPONSE_'): check(payload,seed,node=node)


@pytest.mark.parametrize('fault',['duplicate','nan','empty_points','wrong_mode','unknown_subject','overlong','bad_character'])
def test_parser_never_repairs_or_silently_drops_untrusted_source_fields(fault):
    source = packet()
    if fault=='empty_points': source['task_points']=[]
    elif fault=='wrong_mode': source['response_mode']='vad_duration'
    elif fault=='unknown_subject': source['subject']='model subject'
    elif fault=='overlong': source['prompt']='x'*1201
    elif fault=='bad_character': source['prompt']='Hidden\x00content'
    content = json.dumps(source)
    if fault=='duplicate': content=content[:-1]+',"subject":"数学"}'
    elif fault=='nan': content=content.replace('"written"','NaN')
    with pytest.raises(ValueError,match='OPEN_RESPONSE_'): parse(content)


def test_external_material_remains_an_explicit_dependency_not_invented_context():
    source = packet()
    source['context_requirement']='external_material'
    assert source_payload(json.dumps(source))['open_response']['context_requirement']=='external_material'
    payload,seed,node = inputs(source)
    with pytest.raises(ValueError,match='OPEN_RESPONSE_MATERIAL_BINDING_REQUIRED'): check(payload,seed,node=node)


def test_profiles_do_not_share_mutable_descriptors_or_claim_transcription_is_written_work():
    written = profile_for('数学',task_family='mathematical_process',response_mode='written')
    written['dimensions'][0]['criterion']='Mutated by a caller'
    spoken = profile_for('数学',task_family='mathematical_process',response_mode='spoken_explanation')
    assert spoken['dimensions'][0]['criterion']!='Mutated by a caller'
    assert spoken['written_authorship_allowed'] is False
    assert spoken['required_origin_fields']==['source_voice_input_id','voice_capture_binding','exact_confirmed_transcript']
    with pytest.raises(ValueError,match='OPEN_RESPONSE_PROFILE_SCOPE_INVALID'):
        profile_for('数学',task_family='scientific_explanation',response_mode='written')


@pytest.mark.parametrize('fault',[None,'profile','version','exact','node','mode','scope'])
def test_future_consumer_reads_profile_from_exact_persisted_item_not_subject_guess(fault):
    source = packet()
    payload,seed,node = inputs(source)
    result = check(payload,seed,node=node)
    rubric = result['expected_answer']
    item = {'knowledge_node_id':node['knowledge_node_id'],'answer_type':'open_response',
        'assessment_mode':'open_response_rubric','expected_answer_json':json.dumps(rubric),'status':'active'}
    if fault=='profile': rubric['review_profile']='english_open'
    elif fault=='version': rubric['profile_version']='old-version'
    elif fault=='exact': rubric['exact_answers']=['a keyword']
    elif fault=='node': item['knowledge_node_id']='other-node'
    elif fault=='mode': item['assessment_mode']='pet_reference_rubric'
    elif fault=='scope': node['subject']='化学'
    item['expected_answer_json']=json.dumps(rubric)
    if fault:
        with pytest.raises(ValueError,match='OPEN_RESPONSE_'): profile_for_item(item,node)
    else:
        profile = profile_for_item(item,node)
        assert profile['subject']=='物理' and profile['original_requirement']=='written_original'
        assert profile['profile_id']==rubric['review_profile']


@pytest.mark.parametrize('bad',[[],{},None,7])
def test_profile_invalid_values_are_controlled_errors(bad):
    with pytest.raises(ValueError,match='OPEN_RESPONSE_PROFILE_SCOPE_INVALID'):
        profile_for(bad,task_family='scientific_explanation',response_mode='written')

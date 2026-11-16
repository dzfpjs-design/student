"""Pure source contract for reviewed school explanations and mathematical work.

The parser alone establishes no
child ownership, source permission, media provenance or independent mastery.
"""
import json

from .local_supply import digest, LocalSupplyError
from .learning_obligations import question_identity
from .open_response_profile import profile_for

SCHEMA = 'reviewed_open_response_source_v1'
MATERIAL_SCHEMA = 'reviewed_open_response_source_v2'
VISUAL_SCHEMA = 'reviewed_open_response_source_v3'
VALIDATOR = 'reviewed_open_response_rubric_source_v1'
FIELDS = {'schema','subject','grade','knowledge_node_id','answer_type','assessment_mode',
          'task_family','response_mode','prompt','task_points','context_requirement'}


class OpenResponseSourceError(LocalSupplyError):
    pass


def require(value, code='OPEN_RESPONSE_SOURCE_INVALID'):
    if not value:
        raise OpenResponseSourceError(code)


def _object(pairs):
    result = {}
    for key,value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('OPEN_RESPONSE_NONFINITE_SOURCE')


def _text(value,limit):
    require(isinstance(value,str) and 0<len(value.strip())<=limit
        and not any(ord(char)<32 and char not in '\n\r\t' for char in value))


def parse(content):
    require(isinstance(content,str) and len(content)<=16000)
    try:
        source = json.loads(content,object_pairs_hook=_object,parse_constant=_constant)
    except (ValueError,TypeError) as error:
        raise OpenResponseSourceError('OPEN_RESPONSE_TYPED_SOURCE_REQUIRED') from error
    require(isinstance(source,dict))
    if source.get('schema') != SCHEMA or source.get('context_requirement') != 'self_contained':
        from .rubric_calibration_transaction import unsupported_dependency
        unsupported_dependency()
    visual = source.get('schema') == VISUAL_SCHEMA
    material = source.get('schema') in {MATERIAL_SCHEMA, VISUAL_SCHEMA}
    require(set(source)==FIELDS | ({'material_bindings','material_semantics'} if material else set()))
    require(source['schema'] in {SCHEMA,MATERIAL_SCHEMA,VISUAL_SCHEMA}
        and source['grade'] in ('初三','四年级')
        and source['answer_type']=='open_response' and source['assessment_mode']=='open_response_rubric')
    for key in ('subject','knowledge_node_id','task_family','response_mode'):
        _text(source[key],200)
    _text(source['prompt'],1200)
    points = source['task_points']
    require(isinstance(points,list) and 1<=len(points)<=12)
    for point in points:
        _text(point,400)
    require(len(set(points))==len(points))
    require(source['context_requirement'] in ('self_contained','external_material'))
    if material:
        from .open_response_materials import validate_bindings
        require(source['context_requirement']=='external_material')
        require(source['material_semantics'] in {'text_only','image_required'})
        try:
            if visual:
                require(source['material_semantics']=='image_required')
                from .open_response_materials import validate_visual_bindings
                validate_visual_bindings(source['material_bindings'])
            else:
                validate_bindings(source['material_bindings'])
        except LocalSupplyError as error:
            raise OpenResponseSourceError(str(error)) from error
    try:
        profile_for(source['subject'],task_family=source['task_family'],response_mode=source['response_mode'],
                    **({'grade':source['grade'], 'source_schema':source['schema']} if visual or source['grade']=='四年级' else {}))
    except ValueError as error:
        raise OpenResponseSourceError(str(error)) from error
    return source


def rubric(source):
    source = parse(json.dumps(source,ensure_ascii=False))
    visual = source['schema']==VISUAL_SCHEMA
    profile = profile_for(source['subject'],task_family=source['task_family'],response_mode=source['response_mode'],
                          **({'grade':source['grade'], 'source_schema':source['schema']} if visual or source['grade']=='四年级' else {}))
    result = {'task_points':list(source['task_points']),'review_profile':profile['profile_id'],
        'profile_version':profile['version'],'task_family':source['task_family'],
        'response_mode':source['response_mode'],'context_requirement':source['context_requirement']}
    if source['schema'] in {MATERIAL_SCHEMA,VISUAL_SCHEMA}:
        result.update(material_schema=source['schema'], material_bindings=source['material_bindings'],
                      material_semantics=source['material_semantics'])
    return result


def source_payload(content):
    source = parse(content)
    return {'prompt':source['prompt'],'expected_answer':None,'open_response':source}


def check(payload,seed,*,node,connection=None,child_id=None):
    """Caller still must validate real source/review/child and publication scope.

    `node` and `seed` come from that validator's database transaction, never
    request-provided node objects. Missing material binding stays explicit.
    """
    require(isinstance(payload,dict) and (seed is None or isinstance(seed,dict)) and isinstance(node,dict))
    source = parse(json.dumps(payload.get('open_response'),ensure_ascii=False))
    require(node.get('status')=='active' and node.get('track')=='school'
        and (node.get('subject'),node.get('grade_band'),node.get('knowledge_node_id'))==
            (source['subject'],source['grade'],source['knowledge_node_id']),
        'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
    if seed is None:
        # The source-first route has no diagnostic to compare with. It may only
        # establish a baseline for an actual owned school skill, never transfer.
        require(connection is not None and child_id and payload.get('seed_item_id','missing') is None
            and payload.get('stage')=='parallel', 'OPEN_RESPONSE_FIRST_BASELINE_REQUIRED')
        actual = connection.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
            (source['knowledge_node_id'],)).fetchone()
        child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'",
            (child_id,)).fetchone()
        require(actual and child and actual['node_type']=='skill' and child['grade']==source['grade']
            and all(actual[k]==node[k] for k in ('knowledge_node_id','subject','grade_band','track','status')),
            'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
        return _check_reference(payload,source,None,connection=connection,child_id=child_id)
    if source['schema']==VISUAL_SCHEMA:
        require(connection is not None and child_id, 'VISUAL_MATERIAL_LIVE_SOURCE_REQUIRED')
        actual = connection.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
            (source['knowledge_node_id'],)).fetchone()
        require(actual and all(actual[k]==node[k] for k in ('knowledge_node_id','subject','grade_band','track','status')),
                'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
        return check_reference(payload,seed,connection=connection,child_id=child_id)
    return check_reference(payload,seed)


def check_reference(payload,seed,*,connection=None,child_id=None):
    """Content check only; publication must also run the live node/source proof."""
    require(isinstance(seed,dict), 'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
    source = parse(json.dumps(payload.get('open_response'),ensure_ascii=False))
    return _check_reference(payload,source,seed,connection=connection,child_id=child_id)


def _check_reference(payload,source,seed,*,connection=None,child_id=None):
    required = {'validator','stage','prompt','expected_answer','open_response'}
    common = {'seed_item_id','explanation','difficulty','excerpt_id','requirement_id','standard_page','standard_quote'}
    if seed is None:
        common.add('knowledge_node_id')
        require(payload.get('knowledge_node_id')==source['knowledge_node_id'], 'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
    require(required<=set(payload) and set(payload)<=required|common)
    require(payload['validator']==VALIDATOR and payload['stage'] in ('parallel','near_transfer','far_transfer')
        and payload['expected_answer'] is None and payload['prompt']==source['prompt'],'OPEN_RESPONSE_SOURCE_CONTENT_CHANGED')
    if seed is not None:
        require(seed.get('knowledge_node_id')==source['knowledge_node_id']
            and seed.get('answer_type') in ('text','open_response')
            and ('seed_item_id' not in payload or payload['seed_item_id']==seed.get('diagnostic_item_id')),
            'OPEN_RESPONSE_SOURCE_SCOPE_CHANGED')
    if source['schema']==VISUAL_SCHEMA:
        require(connection is not None and child_id, 'VISUAL_MATERIAL_LIVE_SOURCE_REQUIRED')
        from .open_response_materials import resolve_source_materials
        material = resolve_source_materials(connection,child_id=child_id,source=source)
    elif source['schema']==MATERIAL_SCHEMA:
        require(source['material_semantics']=='text_only','OPEN_RESPONSE_IMAGE_SEMANTICS_REQUIRED')
    else:
        require(source['context_requirement']=='self_contained','OPEN_RESPONSE_MATERIAL_BINDING_REQUIRED')
    if seed is not None:
        require(question_identity({'prompt':source['prompt']})!=question_identity({'prompt':seed['prompt']}),
            'OPEN_RESPONSE_DISTINCT_SOURCE_REQUIRED')
    return {**({'visual_materials':material} if source['schema']==VISUAL_SCHEMA else {}),
        'validator':VALIDATOR,'answer_type':'open_response','assessment_mode':'open_response_rubric',
        'computed_answer':'主观开放题：审核原始参考合同，不作独立复算或能力认证。',
        'answer_verification':'source_reference_review_only',
        'question_hash':digest({'prompt':source['prompt'],'material_bindings':source['material_bindings']})
            if source['schema']==VISUAL_SCHEMA else digest(source['prompt']),'payload_hash':digest(payload),
        'expected_answer':rubric(source),'open_response':source,
        'qualification':'original_reference_checked_not_computed_or_skill_certified',
        'mastery_claim_allowed':False,'stage_qualification_allowed':False,
        'local_only':True,'cloud_transmission_allowed':False}


def candidate_source(connection,candidate):
    """Read the typed candidate identity, not permission or qualification."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
        return None
    row = connection.execute('SELECT payload_json FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?',
        (candidate['candidate_id'],candidate['child_id'])).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    if payload.get('validator') != VALIDATOR:
        require('open_response' not in payload,'OPEN_RESPONSE_SOURCE_CONTENT_CHANGED')
        return None
    return parse(json.dumps(payload.get('open_response'),ensure_ascii=False))

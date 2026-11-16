"""Original subjective writing tasks in the existing reviewed source journal."""
import json

from .local_supply import LocalSupplyError, digest

SCHEMA = 'pet_writing_source_v1'
VALIDATOR = 'pet_writing_rubric_source_v1'
FIELDS = {'schema','modality','assessment_mode','subject','grade','knowledge_node_id','prompt',
          'task_points','min_words','max_words'}


def require(value, code='PET_WRITING_SOURCE_CONTRACT_INVALID'):
    if not value:
        raise LocalSupplyError(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def parse(text):
    require(isinstance(text, str) and len(text) <= 16000)
    try:
        source = json.loads(text, object_pairs_hook=_object)
    except (ValueError, TypeError) as error:
        raise LocalSupplyError('PET_WRITING_TYPED_SOURCE_REQUIRED') from error
    require(isinstance(source, dict) and set(source) == FIELDS)
    require(source['schema'] == SCHEMA and source['modality'] == 'writing'
            and source['assessment_mode'] == 'human_rubric' and source['subject'] == '英语')
    for field, limit in (('prompt',1200),('grade',100),('knowledge_node_id',200)):
        require(isinstance(source[field], str) and 1 <= len(source[field].strip()) <= limit)
    points = source['task_points']
    require(isinstance(points, list) and 1 <= len(points) <= 12
            and all(isinstance(v,str) and 1 <= len(v.strip()) <= 400 for v in points)
            and len(set(points)) == len(points))
    require(type(source['min_words']) is int and type(source['max_words']) is int
            and 1 <= source['min_words'] <= source['max_words'] <= 1500)
    return source


def rubric(source):
    return {'task_points':source['task_points'], 'min_words':source['min_words'], 'max_words':source['max_words'],
            'review_profile':'pet_writing_text_v1'}


def source_payload(text):
    source = parse(text)
    return {'prompt':source['prompt'],'expected_answer':None,'pet_writing':source}


def check(payload, seed):
    require(payload.get('validator') == VALIDATOR and payload.get('stage') in {'parallel','near_transfer','far_transfer'})
    source = parse(json.dumps(payload.get('pet_writing')))
    require(payload.get('prompt') == source['prompt'] and payload.get('expected_answer') is None
            and source['knowledge_node_id'] == seed['knowledge_node_id'] and seed['answer_type'] == 'writing',
            'PET_WRITING_SOURCE_CONTENT_CHANGED')
    require(' '.join(source['prompt'].split()) != ' '.join(seed['prompt'].split()), 'PET_WRITING_DISTINCT_SOURCE_REQUIRED')
    return {'validator':VALIDATOR, 'computed_answer':'主观写作任务，无唯一标准答案',
        'question_hash':digest(source['prompt']), 'payload_hash':digest(payload),
        'expected_answer':rubric(source),'pet_writing':source,
        'qualification':'original_reference_checked_not_computed_or_skill_certified'}


def candidate_writing_source(c, candidate):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
        return None
    row = c.execute('SELECT payload_json FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?',
                    (candidate['candidate_id'],candidate['child_id'])).fetchone()
    if not row:
        return None
    payload = json.loads(row[0])
    if payload.get('validator') != VALIDATOR:
        return None
    source = parse(json.dumps(payload.get('pet_writing')))
    require(candidate['track'] == 'pet' and candidate['prompt'] == source['prompt']
            and json.loads(candidate['expected_answer_json']) == rubric(source), 'PET_WRITING_PUBLISHED_CONTENT_CHANGED')
    return source

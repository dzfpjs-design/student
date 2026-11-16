"""Whole-answer original text sources for the existing independent review journal.

Structural validation is not independent recomputation, pedagogical approval,
audio assessment or mastery. No permissions or external media enter this schema.
"""
import json
import unicodedata

from .local_supply import LocalSupplyError, digest

VALIDATOR = 'curriculum_objective_exact_source_v1'
SCHEMA = 'curriculum_objective_source_v1'
FIELDS = {'schema', 'subject', 'grade', 'knowledge_node_id', 'assessment_mode',
          'answer_type', 'prompt', 'expected_answer'}


def _require(value):
    if not value:
        raise LocalSupplyError('CURRICULUM_OBJECTIVE_SOURCE_INVALID')


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def normalize(value):
    _require(isinstance(value, str))
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def parse(content):
    _require(isinstance(content, str) and len(content) <= 16000)
    try:
        packet = json.loads(content, object_pairs_hook=_object)
    except (ValueError, TypeError) as error:
        raise LocalSupplyError('CURRICULUM_OBJECTIVE_TYPED_SOURCE_REQUIRED') from error
    _require(isinstance(packet, dict) and set(packet) == FIELDS)
    _require(packet['schema'] == SCHEMA and packet['assessment_mode'] == 'objective'
             and packet['answer_type'] == 'text')
    for field in ('subject', 'grade', 'knowledge_node_id', 'prompt'):
        value = packet[field]
        _require(isinstance(value, str) and 0 < len(value.strip()) <= (1200 if field == 'prompt' else 100)
                 and not any(ord(char) < 32 and char not in '\n\r\t' for char in value))
    rubric = packet['expected_answer']
    _require(isinstance(rubric, dict) and rubric.get('local_rule') == 'local_exact_v1')
    if rubric.get('answer_format')=='reviewed_spoken_numeric_tuple_v1':
        from .spoken_numeric_tuple import validate
        try:validate(rubric)
        except (ValueError,TypeError,KeyError,ZeroDivisionError):_require(False)
    else:
        _require(set(rubric)=={'local_rule','exact_answers'})
    answers = rubric['exact_answers']
    _require(isinstance(answers, list) and 1 <= len(answers) <= 24
             and all(isinstance(value, str) and 0 < len(value.strip()) <= 200 for value in answers))
    _require(all(normalize(value) for value in answers) and len({normalize(value) for value in answers}) == len(answers))
    return packet


def node_allowed(node, seed):
    code = node['code'].upper()
    return (seed['answer_type'] == 'text' and node['track'] in {'school', 'competition_math', 'pet'}
            and not any(part in code.split('.') for part in ('LISTENING', 'SPEAKING', 'WRITING', 'PHONICS')))


def source_payload(content):
    original = parse(content)
    return {'prompt': original['prompt'], 'expected_answer': None, 'objective_source': original}


def exact_rubric(original):
    if original['expected_answer'].get('answer_format')=='reviewed_spoken_numeric_tuple_v1':
        from .spoken_numeric_tuple import validate
        validate(original['expected_answer'])
        return dict(original['expected_answer'])
    return {'local_rule': 'local_exact_v1', 'answer_format': 'reviewed_exact_text_v1',
            'exact_answers': [normalize(value) for value in original['expected_answer']['exact_answers']]}


def check(payload, seed):
    original = parse(json.dumps(payload.get('objective_source')))
    _require(payload['validator'] == VALIDATOR and payload['stage'] == 'parallel'
             and payload['expected_answer'] is None and payload['prompt'] == original['prompt']
             and payload['prompt'] != seed['prompt'] and original['knowledge_node_id'] == seed['knowledge_node_id'])
    return {'validator': VALIDATOR, 'computed_answer': original['expected_answer']['exact_answers'][0],
            'question_hash': digest(payload['prompt']), 'payload_hash': digest(payload),
            'expected_answer': exact_rubric(original),
            'qualification': 'original_reference_checked_not_computed_or_skill_certified'}

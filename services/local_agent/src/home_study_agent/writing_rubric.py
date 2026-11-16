"""Explicit original-writing review. Generic English text is not this profile."""
from datetime import datetime, timezone
import json

from .local_supply import digest
from .writing_response_origin import original_written_response, require, writing_item

PROFILE = 'pet_writing_text_v1'
OPEN_PROFILE = 'school_open_written_v1'
REVIEW_PROFILES = {PROFILE, OPEN_PROFILE}


def is_writing(c, item_id):
    row = c.execute('''SELECT kn.track,kn.code,di.answer_type,di.assessment_mode,di.expected_answer_json FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?''', (item_id,)).fetchone()
    if row and row['answer_type']=='open_response':
        try:
            return row['assessment_mode']=='open_response_rubric' and json.loads(row['expected_answer_json']).get('response_mode')=='written'
        except (ValueError,TypeError,AttributeError):
            return False
    return bool(row and row['track'] == 'pet' and row['code'].startswith('PET.WRITING.') and row['answer_type'] == 'writing')


def writing_dimensions(c, child_id, item_id):
    item,node = writing_item(c,child_id,item_id)
    if item['answer_type']=='open_response':
        from .open_response_profile import profile_for_item
        return profile_for_item(item,node)['dimensions']
    from .learning_rubric import rubric_dimensions
    return rubric_dimensions('英语')


def prepare_writing_review(c, child_id, assessment_id):
    from .learning_rubric import RubricEvidenceService
    from .learning_obligations import withdrawn_evidence_ids
    current = RubricEvidenceService(None).get(child_id, assessment_id, connection=c)
    require(current['source_status'] == 'current' and current['source_evidence_id']
            and current['source_evidence_id'] not in withdrawn_evidence_ids(c, child_id), 'ASSESSMENT_SOURCE_CHANGED')
    attempts = c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=? AND child_id=?',
                         (current['source_evidence_id'], child_id)).fetchall()
    require(len(attempts) == 1, 'EXACT_ATTEMPT_REQUIRED')
    attempt = attempts[0]
    prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
                        (child_id, attempt['attempt_id'])).fetchall()
    require(len(prompts) == 1, 'EXACT_PROMPT_REQUIRED')
    item, node = writing_item(c, child_id, attempt['diagnostic_item_id'])
    require(current['material'].get('diagnostic_item_id') == item['diagnostic_item_id']
            and current['session_id'] == attempt['session_id'] and current['response'] == attempt['submitted_answer']
            and current['question'] == item['prompt'], 'ASSESSMENT_ATTEMPT_MISMATCH')
    original = original_written_response(c, prompts[0], attempt)
    binding = {'profile': OPEN_PROFILE if item['answer_type']=='open_response' else PROFILE, 'assessment_id': assessment_id, 'assessment_fingerprint': current['fingerprint'],
        'original': original, 'prompt_id': prompts[0]['prompt_id'], 'item_version': prompts[0]['item_version'],
        'attempt_id': attempt['attempt_id'], 'evidence_id': attempt['evidence_id'],
        'diagnostic_item_id': item['diagnostic_item_id'], 'knowledge_node_id': node['knowledge_node_id']}
    return {**binding, 'binding_version': digest(binding), 'task_points':json.loads(item['expected_answer_json'])['task_points'],
            'local_only': True, 'cloud_transmission_allowed': False}


def validate_writing_review(c, child_id, assessment_id, *, binding_version):
    current = prepare_writing_review(c, child_id, assessment_id)
    require(current['binding_version'] == binding_version, 'REVIEW_BINDING_CHANGED')
    return current


def validate_task_point_evidence(binding, evidence, response):
    from .learning_rubric import RubricEvidenceService
    require(isinstance(evidence, list) and len(evidence) == len(binding['task_points'])
        and all(isinstance(e, dict) and set(e) == {'task_point','response_quote'}
            and isinstance(e['task_point'], str) and e['task_point'] in binding['task_points']
            and RubricEvidenceService._quotes([e['response_quote']], response) for e in evidence)
        and {e['task_point'] for e in evidence} == set(binding['task_points']), 'TASK_POINT_EVIDENCE_REQUIRED')


def current_writing_review(c, child_id, current):
    if not has_writing_review_evidence(current):
        return False
    review = current['review']
    evidence = review['writing_evidence']
    try:
        binding = validate_writing_review(c, child_id, current['assessment_id'], binding_version=evidence['binding_version'])
        if review['review_profile']!=binding['profile']:
            return False
        validate_task_point_evidence(binding, evidence['task_point_evidence'], current['response'])
        definitions = {d['dimension_id']: d for d in writing_dimensions(c,child_id,binding['diagnostic_item_id'])}
        scores = review['dimensions']
        return (len(scores) == len(definitions) and {d['dimension_id'] for d in scores} == set(definitions)
            and all(type(d['score']) is int and 0 <= d['score'] <= definitions[d['dimension_id']]['max_score'] for d in scores)
            and review['passed'] == all(d['score'] >= definitions[d['dimension_id']]['pass_score'] for d in scores))
    except (ValueError, KeyError, TypeError):
        return False


def writing_review_gate(c, child_id, evidence_id, *, as_of=None):
    from .learning_rubric import RubricEvidenceService, _time
    unknown = {'status': 'unknown', 'skill_scoring_allowed': False, 'blockers': ['writing_original_review_required']}
    service = RubricEvidenceService(None)
    if not service.available(c):
        return unknown
    from .writing_score import written_score_candidates
    try:
        currents = written_score_candidates(c, child_id, evidence_id)
    except (ValueError, KeyError, TypeError):
        return unknown
    reviewed = [v for v in currents if v['review']]
    if not reviewed:
        from .writing_score import current_written_score
        now = _time(as_of) if as_of else datetime.now(timezone.utc)
        qualified = []
        for candidate in currents:
            try:
                score = current_written_score(c, child_id, candidate['assessment_id'], now=now)
            except (ValueError, KeyError, TypeError):
                continue
            qualified.append(score)
        if len(qualified) != 1:
            return unknown
        score = qualified[0]
        receipt, decision = score['receipt'], score['decision']
        passed = decision['passed'] and receipt['independent'] and receipt['hint_level'] == 0
        return {'status': decision['status'], 'skill_scoring_allowed': bool(passed),
            'blockers': [] if passed else ['writing_score_not_independent_correct'],
            'assessment_scope': 'calibrated_model_original_writing',
            'assessment_id': score['current']['assessment_id'], 'review_revision': 0,
            'score_authority_id': decision['authority_id'],
            'original_binding_version': score['original_binding_version']}
    current = max(reviewed, key=lambda v: (_time(v['review']['created_at']), v['review']['event_id']))
    now = _time(as_of) if as_of else datetime.now(timezone.utc)
    if _time(current['review']['created_at']) > now or not current_writing_review(c, child_id, current):
        return unknown
    review = current['review']
    passed = review['passed'] and review['independent'] and review['hint_level'] == 0
    return {'status': 'reviewed_human', 'skill_scoring_allowed': bool(passed),
        'blockers': [] if passed else ['writing_review_not_independent_correct'],
        'assessment_scope': 'reviewed_human_original_writing', 'assessment_id': current['assessment_id'],
        'review_revision': current['review_revision']}


def has_writing_review_evidence(current):
    review = current.get('review')
    if not review or review.get('operation') != 'approve' or review.get('review_profile') not in REVIEW_PROFILES:
        return False
    evidence = review.get('writing_evidence')
    return isinstance(evidence, dict) and set(evidence) == {'binding_version','task_point_evidence'}

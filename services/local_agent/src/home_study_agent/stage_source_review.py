"""Explicit pedagogical stage review on the existing source/candidate journal."""
import json

from .local_supply import LocalSupplyError, digest, item_snapshot, validate_candidate_source

SCHEMA = 'specialized_stage_review_v1'
FIELDS = {'schema', 'stage', 'binding_version', 'source_quote', 'candidate_quote',
          'same_skill_rationale', 'changed_context_rationale'}


def stage_review_context(c, child_id, candidate_id):
    row = c.execute('SELECT * FROM specialized_supply_candidate WHERE candidate_id=? AND child_id=?',
                    (candidate_id, child_id)).fetchone()
    if not row:
        raise KeyError(candidate_id)
    job = c.execute('SELECT generation_mode FROM specialized_supply_job WHERE job_id=? AND child_id=?',
                    (row['job_id'], child_id)).fetchone()
    if job and job['generation_mode'] == 'curriculum_review':
        from .curriculum_supply import _evidence, _check, _source_snapshot_matches
        package = c.execute('SELECT * FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?',
                            (candidate_id, child_id)).fetchone()
        if not package or package['state'] not in {'validated', 'reviewed', 'published'}:
            raise LocalSupplyError('STAGE_REVIEW_VALIDATED_SOURCE_REQUIRED')
        payload = json.loads(package['payload_json'])
        proof = _evidence(c, child_id, payload, candidate_id=candidate_id)
        if not _source_snapshot_matches(proof, json.loads(package['source_snapshot_json'])):
            raise LocalSupplyError('STAGE_REVIEW_SOURCE_CONFLICT')
        _check(payload, proof['seed'], connection=c, child_id=child_id)
        seed = proof['seed']
    else:
        seed = validate_candidate_source(c, 'specialized_supply_candidate', row)
    candidate = {key: row[key] for key in ('candidate_id', 'child_id', 'seed_item_id', 'stage', 'prompt',
        'expected_answer_json', 'explanation', 'difficulty', 'content_fingerprint')}
    return {'schema': SCHEMA, 'child_id': child_id, 'candidate_id': candidate_id,
        'binding_version': SCHEMA + ':' + digest({'candidate': candidate, 'source': item_snapshot(seed)}),
        'source_root_item_id': seed['diagnostic_item_id'], 'knowledge_node_id': seed['knowledge_node_id'],
        'source_prompt': seed['prompt'], 'candidate_prompt': row['prompt'],
        'candidate_explanation': row['explanation'], 'status': row['status'],
        'allowed_stages': ['near_transfer', 'far_transfer'],
        'qualification_scope': 'explicit_human_source_stage_review_not_child_mastery',
        'local_only': True, 'cloud_transmission_allowed': False}


def validate_stage_review(c, child_id, candidate_id, request):
    if (not isinstance(request, dict) or set(request) != FIELDS or request['schema'] != SCHEMA
            or request['stage'] not in {'near_transfer', 'far_transfer'}):
        raise LocalSupplyError('STAGE_REVIEW_CONTRACT_INVALID')
    context = stage_review_context(c, child_id, candidate_id)
    if request['binding_version'] != context['binding_version']:
        raise LocalSupplyError('STAGE_REVIEW_SOURCE_CONFLICT')
    for name, source in (('source_quote', 'source_prompt'), ('candidate_quote', 'candidate_prompt')):
        quote = request[name]
        if not isinstance(quote, str) or not 4 <= len(quote.strip()) <= 1200 or quote not in context[source]:
            raise LocalSupplyError('STAGE_REVIEW_ORIGINAL_QUOTE_REQUIRED')
    for name in ('same_skill_rationale', 'changed_context_rationale'):
        if not isinstance(request[name], str) or not 12 <= len(request[name].strip()) <= 1200:
            raise LocalSupplyError('STAGE_REVIEW_EXPLICIT_RATIONALE_REQUIRED')
    if ' '.join(context['source_prompt'].split()) == ' '.join(context['candidate_prompt'].split()):
        raise LocalSupplyError('STAGE_REVIEW_DISTINCT_QUESTION_REQUIRED')
    return context


def reviewed_stage(c, child_id, candidate):
    value = json.loads(candidate['validation_json']).get('stage_review')
    if not value or candidate['status'] != 'promoted' or not candidate['review_action_id'] or not candidate['review_note']:
        return None
    if candidate['review_action_id'].startswith('local-qualification:'):
        return None
    context = validate_stage_review(c, child_id, candidate['candidate_id'], value)
    if candidate['review_action_id'].startswith('curriculum-publish:'):
        event = c.execute("SELECT result_json FROM curriculum_supply_event WHERE candidate_id=? AND child_id=? AND kind='review' ORDER BY version DESC LIMIT 1",
                          (candidate['candidate_id'], child_id)).fetchone()
        if not event or json.loads(event['result_json']).get('stage_review') != value:
            raise LocalSupplyError('STAGE_REVIEW_RECEIPT_CONFLICT')
    return {**context, 'stage': value['stage'], 'review_action_id': candidate['review_action_id'],
            'reviewed_at': candidate['reviewed_at'], 'review_basis': value}


def require_mistake_stage(c, child_id, item, candidate, stage, mistake_id):
    """Admit only an explicitly reviewed, exact-scored transfer of this mistake.

    The caller has already validated the candidate's current source/snapshot.
    Keep this check in source validation so evidence replay uses the same gate
    as new attempts, without recursively validating this item again.
    """
    from datetime import datetime, timezone
    from .teaching_context import _time
    if (stage not in {'near_transfer', 'far_transfer'} or not mistake_id
            or not item['source_ref'].startswith('parent_approved_specialized:')):
        raise LocalSupplyError('SUPPLY_STAGE_OR_MISTAKE_MISMATCH')
    reviewed_at = _time(candidate['reviewed_at'])
    if not reviewed_at or reviewed_at > datetime.now(timezone.utc):
        raise LocalSupplyError('STAGE_REVIEW_DATE_INVALID')
    rubric = json.loads(item['expected_answer_json'])
    if (item['answer_type'] != 'text' or item['assessment_mode'] != 'objective'
            or rubric.get('local_rule') != 'local_exact_v1' or not rubric.get('exact_answers')
            or rubric.get('keyword_groups') or rubric.get('task_points')):
        raise LocalSupplyError('STAGE_REVIEW_EXACT_SCORE_REQUIRED')
    explicit = reviewed_stage(c, child_id, candidate)
    case = c.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=? AND status!='excluded'",
                     (mistake_id, child_id)).fetchone()
    if (not explicit or explicit['stage'] != stage or not case
            or explicit['knowledge_node_id'] != item['knowledge_node_id']
            or case['knowledge_node_id'] != item['knowledge_node_id']
            or explicit['source_root_item_id'] != case['diagnostic_item_id']):
        raise LocalSupplyError('SUPPLY_STAGE_OR_MISTAKE_MISMATCH')
    return explicit

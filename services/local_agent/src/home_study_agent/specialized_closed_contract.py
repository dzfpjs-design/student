"""Typed closed references, never model calibration or pedagogical approval.

Only the existing parent review authorizes publication. Validation checks the
whole-answer scoring contract and source binding, not the model's correctness.
"""
import json

from . import curriculum_objective_source as objective
from .local_supply import LocalSupplyError, digest, item_snapshot
from . import spoken_numeric

VERSION = 'specialized_closed_exact_v1'
QUALIFICATION = 'structural_contract_only_parent_reference_review_required'


def require_seed(seed, node):
    if not objective.node_allowed(node, seed) or seed['assessment_mode'] != 'objective':
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED')


def parse(context, prompt, contract):
    numeric = isinstance(contract, dict) and contract.get('schema') == spoken_numeric.SCHEMA
    fields = {'schema', 'answer_type', 'assessment_mode', 'expected_answer'}
    if (not isinstance(contract, dict)
            or set(contract) != fields | ({'numeric_kind'} if numeric else set())
            or contract['schema'] not in {VERSION, spoken_numeric.SCHEMA}):
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_CONTRACT_REQUIRED')
    packet = {'schema': objective.SCHEMA, 'subject': context['subject'],
              'grade': context['grade_band'], 'knowledge_node_id': context['knowledge_node_id'],
              'answer_type': contract['answer_type'], 'assessment_mode': contract['assessment_mode'],
              'prompt': prompt, 'expected_answer': contract['expected_answer']}
    packet = objective.parse(json.dumps(packet, ensure_ascii=False))
    if numeric:
        packet.update(schema=spoken_numeric.SCHEMA, numeric_kind=contract['numeric_kind'])
    return parse_packet(packet)


def parse_packet(packet):
    if isinstance(packet, dict) and packet.get('schema') == spoken_numeric.SCHEMA:
        if set(packet) != objective.FIELDS | {'numeric_kind'}:
            raise LocalSupplyError('SPECIALIZED_NUMERIC_CONTRACT_INVALID')
        original = objective.parse(json.dumps({**{k: v for k, v in packet.items() if k != 'numeric_kind'},
                                               'schema': objective.SCHEMA}))
        try:
            if not spoken_numeric.task_supported(original['prompt'], packet['numeric_kind']):
                raise ValueError('task requires more than scalar equivalence')
            references = [spoken_numeric.reference(value, packet['numeric_kind'])
                          for value in original['expected_answer']['exact_answers']]
            if len(set(references)) != len(references):
                raise ValueError('duplicate numeric references')
        except (ValueError, TypeError, ZeroDivisionError) as error:
            raise LocalSupplyError('SPECIALIZED_NUMERIC_CONTRACT_INVALID') from error
        return {**original, 'schema': spoken_numeric.SCHEMA, 'numeric_kind': packet['numeric_kind']}
    return objective.parse(json.dumps(packet))


def exact_rubric(packet):
    if packet['schema'] == spoken_numeric.SCHEMA:
        return {'local_rule': 'local_exact_v1', 'answer_format': spoken_numeric.FORMAT,
                'numeric_kind': packet['numeric_kind'],
                'exact_answers': list(packet['expected_answer']['exact_answers'])}
    return objective.exact_rubric(packet)


def fingerprint(candidate, seed):
    return digest({'stage': candidate['stage'], 'prompt': candidate['prompt'],
                   'expected_answer': candidate['expected_answer'], 'explanation': candidate['explanation'],
                   'difficulty': candidate['difficulty'], 'seed': item_snapshot(seed)})


def validate_candidate(connection, candidate, seed, job):
    node = connection.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                              (seed['knowledge_node_id'],)).fetchone()
    if node is None:
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED')
    require_seed(seed, node)
    validation = json.loads(candidate['validation_json'])
    if (validation.get('ruleset') != VERSION or validation.get('passed') is not True
            or validation.get('qualification') != QUALIFICATION):
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_REVIEW_REQUIRED')
    packet = parse_packet(validation.get('closed_contract'))
    expected = exact_rubric(packet)
    if (packet['prompt'] != candidate['prompt']
            or (packet['subject'], packet['grade'], packet['knowledge_node_id']) !=
               (node['subject'], node['grade_band'], seed['knowledge_node_id'])
            or candidate['subject'] != node['subject']
            or any(candidate[key] != job[key] for key in ('child_id', 'track', 'domain_id', 'seed_item_id'))
            or candidate['stage'] not in json.loads(job['stages_json'])
            or expected != json.loads(candidate['expected_answer_json'])):
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_CONTENT_CHANGED')
    content = {**dict(candidate), 'expected_answer': expected}
    if validation.get('content_sha256') != fingerprint(content, seed):
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_CONTENT_CHANGED')
    if candidate['status'] == 'promoted' and not (candidate['review_note'] or '').strip():
        raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_REVIEW_REQUIRED')
    if candidate['status'] == 'promoted':
        published = connection.execute('SELECT knowledge_node_id,answer_type,assessment_mode FROM diagnostic_item WHERE diagnostic_item_id=?',
                                       (candidate['diagnostic_item_id'],)).fetchone()
        if published is None or tuple(published) != (seed['knowledge_node_id'], 'text', 'objective'):
            raise LocalSupplyError('SPECIALIZED_TYPED_CLOSED_CONTENT_CHANGED')
    return packet

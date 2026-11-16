"""Exact authored oral tasks. Source review never grants audio or mastery scores."""
import json
import re

from .local_supply import LocalSupplyError, digest, item_snapshot, validate_item

SCHEMA = 'pet_speaking_source_v1'
VALIDATOR = 'pet_speaking_rubric_source_v1'
FIELDS = {'schema', 'subject', 'grade', 'knowledge_node_id', 'answer_type', 'assessment_mode',
          'task_family', 'prompt', 'task_points', 'stimulus_text', 'stimulus_binding'}
FAMILIES = {
    'topic_response': ('PET.SPEAKING.PICTURE', 'speaking'),
    'picture_description': ('PET.SPEAKING.PICTURE', 'speaking'),
    'read_aloud': ('PET.SPEAKING.READ_ALOUD', 'read_aloud'),
    'interaction': ('PET.SPEAKING.INTERACTION', 'speaking'),
    'extended_turn': ('PET.SPEAKING.EXTENDED_TURN', 'speaking'),
    'daily_dialogue': ('PET.COMPONENT.FUNCTION', 'speaking'),
    'retell': ('PET.READING.MAIN_IDEA', 'speaking'),
}


def require(value, code='CONTRACT_INVALID'):
    if not value:
        raise LocalSupplyError('PET_ORAL_SOURCE_' + code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _text(value, maximum):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum
            and not any(ord(char) < 32 and char not in '\n\r\t' for char in value))


def parse(text):
    require(isinstance(text, str) and len(text) <= 16000)
    try:
        source = json.loads(text, object_pairs_hook=_object)
    except (ValueError, TypeError) as error:
        raise LocalSupplyError('PET_ORAL_TYPED_SOURCE_REQUIRED') from error
    require(isinstance(source, dict) and set(source) == FIELDS)
    require(source['schema'] == SCHEMA and source['subject'] == '英语'
            and source['assessment_mode'] == 'pet_reference_rubric'
            and isinstance(source['task_family'], str) and source['task_family'] in FAMILIES)
    family = source['task_family']
    require(source['answer_type'] == FAMILIES[family][1])
    for key, maximum in (('grade', 100), ('knowledge_node_id', 200), ('prompt', 1200)):
        _text(source[key], maximum)
    points = source['task_points']
    require(isinstance(points, list) and 1 <= len(points) <= 12)
    for point in points:
        _text(point, 400)
    require(len({point.strip() for point in points}) == len(points))
    if source['stimulus_text'] is not None:
        _text(source['stimulus_text'], 6000)
    if family in {'read_aloud', 'retell'}:
        require(source['stimulus_text'] is not None, 'COMPLETE_TEXT_REQUIRED')
    if family == 'picture_description':
        binding = source['stimulus_binding']
        require(isinstance(binding, dict) and set(binding) == {
            'diagnostic_item_id', 'source_version', 'stimulus_asset'}, 'PICTURE_BINDING_REQUIRED')
        for key in ('diagnostic_item_id', 'stimulus_asset'):
            _text(binding[key], 200)
        require(isinstance(binding['source_version'], str)
                and re.fullmatch('[0-9a-f]{64}', binding['source_version']), 'PICTURE_BINDING_INVALID')
    else:
        require(source['stimulus_binding'] is None, 'UNEXPECTED_PICTURE')
    return source


def node_allowed(source, node, seed):
    return (node['status'] == 'active' and node['track'] == 'pet'
            and node['subject'] == source['subject']
            and node['grade_band'] == source['grade'] + '-PET'
            and node['knowledge_node_id'] == source['knowledge_node_id'] == seed['knowledge_node_id']
            and (node['code'], seed['answer_type']) == FAMILIES[source['task_family']])


def bind_stimulus(c, child_id, item_id):
    """Read-only descriptor of an existing qualified picture, not a media grant."""
    origin = c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()
    # Existing picture delivery uses shipped renderer assets. A generated item
    # must not form an unbounded material dependency chain or invent an asset.
    require(origin and (origin['source_ref'] or '').startswith('hsc_original'), 'PICTURE_ORIGINAL_REQUIRED')
    seed = validate_item(c, child_id, item_id)
    require(seed['answer_type'] == 'speaking' and seed['stimulus_asset'], 'PICTURE_UNAVAILABLE')
    return dict(diagnostic_item_id=item_id, source_version=digest(item_snapshot(seed)),
                stimulus_asset=seed['stimulus_asset'])


def materials(c, child_id, source):
    binding = source['stimulus_binding']
    proof = None
    if binding:
        # Only an explicitly bound, validated original picture may supply the
        # existing renderer asset. Never inherit the chosen seed's picture.
        actual = bind_stimulus(c, child_id, binding['diagnostic_item_id'])
        require(actual == binding, 'PICTURE_CHANGED')
        item = validate_item(c, child_id, binding['diagnostic_item_id'])
        require(item['knowledge_node_id'] == source['knowledge_node_id'], 'PICTURE_SCOPE_CHANGED')
        proof = item_snapshot(item)
    return dict(stimulus_text=source['stimulus_text'],
                stimulus_asset=binding['stimulus_asset'] if binding else None, picture_source=proof)


def rubric(source):
    return dict(task_points=source['task_points'], task_family=source['task_family'], source_schema=SCHEMA,
                reference_scope='oral_content_reference_only')


def source_payload(text):
    source = parse(text)
    return dict(prompt=source['prompt'], expected_answer=None, pet_oral=source)


def check(payload, seed):
    source = parse(json.dumps(payload.get('pet_oral'), ensure_ascii=False))
    require(payload.get('validator') == VALIDATOR and payload.get('stage') == 'parallel', 'STAGE_UNAVAILABLE')
    require(payload.get('prompt') == source['prompt'] and payload.get('expected_answer') is None
            and source['knowledge_node_id'] == seed['knowledge_node_id']
            and source['answer_type'] == seed['answer_type'], 'CONTENT_CHANGED')
    from .learning_obligations import question_identity
    require(question_identity(dict(prompt=source['prompt'], stimulus_text=source['stimulus_text'],
                                  stimulus_asset=(source['stimulus_binding'] or {}).get('stimulus_asset')))
            != question_identity(seed), 'DISTINCT_SOURCE_REQUIRED')
    return dict(validator=VALIDATOR, computed_answer='口语原题与参考任务点，无唯一标准答案；不认证发音、流利度或掌握。',
        question_hash=digest(source['prompt']), payload_hash=digest(payload), expected_answer=rubric(source),
        pet_oral=source, qualification='original_reference_checked_not_computed_or_skill_certified',
        mastery_claim_allowed=False, stage_qualification_allowed=False, local_only=True, cloud_transmission_allowed=False)


def candidate_source(c, candidate):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
        return None
    row = c.execute('SELECT payload_json FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?',
                    (candidate['candidate_id'], candidate['child_id'])).fetchone()
    if not row:
        return None
    payload = json.loads(row[0])
    if payload.get('validator') != VALIDATOR:
        return None
    source = parse(json.dumps(payload.get('pet_oral'), ensure_ascii=False))
    require(candidate['track'] == 'pet' and candidate['prompt'] == source['prompt']
            and json.loads(candidate['expected_answer_json']) == rubric(source), 'PUBLISHED_CONTENT_CHANGED')
    return source


def publish(c, candidate, source):
    require(c.in_transaction, 'PUBLICATION_TRANSACTION_REQUIRED')
    from .curriculum_supply import validate_curriculum_candidate
    validate_curriculum_candidate(c, candidate)
    material = materials(c, candidate['child_id'], source)
    item_id = 'specialized-' + candidate['candidate_id']
    inserted = c.execute('''INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,
        expected_answer_json,explanation,assessment_mode,difficulty,source_ref,status,stimulus_text,stimulus_asset)
        SELECT ?,di.knowledge_node_id,?,?,?,?,'pet_reference_rubric',?,?,'active',?,?
        FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'
          AND kn.track='pet' AND kn.code=? AND kn.grade_band=? AND kn.subject=?
          AND kn.knowledge_node_id=? AND di.answer_type=?''',
        (item_id, source['prompt'], source['answer_type'], candidate['expected_answer_json'], candidate['explanation'],
         candidate['difficulty'], 'parent_approved_specialized:' + candidate['candidate_id'],
         material['stimulus_text'], material['stimulus_asset'], candidate['seed_item_id'],
         FAMILIES[source['task_family']][0], source['grade'] + '-PET', source['subject'],
         source['knowledge_node_id'], source['answer_type']))
    require(inserted.rowcount == 1, 'NODE_UNAVAILABLE')
    return item_id


def resolve_read_aloud_reference(c, child_id, item_id, item_version):
    """Read current persisted material; return private identity and transient text.

    Prompt/ASR/confirmation text never supply the reference. This is source
    authority only, not alignment, an installed assessor or learning credit.
    Caller owns the same snapshot/transaction as its original capture binding.
    """
    import hashlib
    from .diagnostic_prompt import DiagnosticPromptService, _version
    item = validate_item(c, child_id, item_id, stage='parallel')
    current = DiagnosticPromptService._item(c, child_id, item_id)
    node = c.execute('SELECT code FROM knowledge_node WHERE knowledge_node_id=?',
                     (item['knowledge_node_id'],)).fetchone()
    require(current is not None and _version(current) == item_version
            and item['answer_type'] == 'read_aloud' and item['assessment_mode'] == 'pet_reference_rubric'
            and node is not None and node[0] == 'PET.SPEAKING.READ_ALOUD', 'REFERENCE_ITEM_CHANGED')
    text = item['stimulus_text']
    _text(text, 6000)
    source_ref = item['source_ref'] or ''
    if source_ref.startswith('hsc_original'):
        origin = dict(kind='original_persisted_item', source_ref=source_ref)
    else:
        prefix = 'parent_approved_specialized:'
        require(source_ref.startswith(prefix), 'REFERENCE_SOURCE_UNAVAILABLE')
        candidate = c.execute("SELECT * FROM specialized_supply_candidate WHERE candidate_id=? AND child_id=? AND diagnostic_item_id=? AND status='promoted'",
            (source_ref[len(prefix):], child_id, item_id)).fetchone()
        require(candidate is not None and candidate['stage'] == 'parallel', 'REFERENCE_PUBLICATION_CHANGED')
        # Reuse the original validator: current source/review/licence/grade/
        # selection, independent review, published item and material all match.
        from .curriculum_supply import validate_curriculum_candidate
        validate_curriculum_candidate(c, candidate)
        package = c.execute("SELECT * FROM curriculum_supply_package WHERE candidate_id=? AND child_id=? AND state='published'",
                            (candidate['candidate_id'], child_id)).fetchone()
        require(package is not None, 'REFERENCE_PUBLICATION_CHANGED')
        payload = json.loads(package['payload_json']); proof = json.loads(package['source_snapshot_json'])
        typed = parse(json.dumps(payload.get('pet_oral'), ensure_ascii=False))
        require(typed['task_family'] == 'read_aloud' and typed['stimulus_text'] == text,
                'REFERENCE_MATERIAL_CHANGED')
        source = proof['source']
        require(source['child_id'] == child_id, 'REFERENCE_SOURCE_CHANGED')
        actual = c.execute('SELECT * FROM knowledge_source WHERE source_id=? AND child_id=?',
                           (source['source_id'], child_id)).fetchone()
        require(actual is not None and actual['current_version'] == 1 and actual['review_status'] == 'approved'
                and actual['content_hash'] == source['content_hash'], 'REFERENCE_SOURCE_CHANGED')
        reviews = [dict(row) for row in c.execute("SELECT * FROM curriculum_supply_event WHERE candidate_id=? AND child_id=? AND kind='review' ORDER BY version",
            (candidate['candidate_id'], child_id)) if json.loads(row['result_json']).get('state') == 'reviewed']
        require(bool(reviews), 'REFERENCE_REVIEW_CHANGED')
        origin = dict(kind='parent_reviewed_curriculum', source_ref=source_ref,
            candidate_id=candidate['candidate_id'], package_version=package['version'],
            source_id=source['source_id'], content_sha256=source['content_hash'],
            excerpt_id=source['excerpt_id'], excerpt_sha256=source['excerpt_hash'],
            revision_sha256=digest({key:actual[key] for key in ('source_id','content_hash','version_label','predecessor_id')}),
            publication_sha256=digest(dict(source_snapshot=proof, payload=payload, review=reviews[-1])))
    identity = dict(schema='pet_read_aloud_reference_v1', child_id=child_id,
        diagnostic_item_id=item_id, item_version=item_version, source=origin,
        item_snapshot_sha256=digest(item_snapshot(item)), text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
        text_utf8_bytes=len(text.encode('utf-8')))
    return identity, text

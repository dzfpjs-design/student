"""Local visual question identity and current review evidence, never cloud data."""
import hashlib
import json
import re

VERSION = 'rubric_visual_material_v1'
FLAGS = dict(local_only=True, cloud_transmission_allowed=False)


def _require(value, code='SOURCE_CHANGED'):
    if not value:
        raise ValueError('RUBRIC_VISUAL_' + code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':')).encode()).hexdigest()


def _display_time(value):
    _require(isinstance(value, str) and bool(value.strip()), 'DISPLAY_CHANGED')
    from .learning_rubric import _time
    try:
        return _time(value)
    except ValueError as error:
        raise ValueError('RUBRIC_VISUAL_DISPLAY_CHANGED') from error


def frozen_visual(c, item):
    """Read publication content, including withdrawn history, without granting use."""
    value = dict(item)
    requested = dict(value)
    expected = value.get('expected_answer_json')
    if isinstance(expected, str):
        expected = json.loads(expected)
    declared = isinstance(expected, dict) and expected.get('material_schema') == 'reviewed_open_response_source_v3'
    if c is None:
        _require(not declared, 'CONNECTION_REQUIRED')
        # Legacy pure text snapshots have no typed provenance. All transactional
        # identity consumers pass their connection so v3 cannot take this path.
        return None
    if c is not None and value.get('diagnostic_item_id') and 'source_ref' not in value:
        row = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                        (value['diagnostic_item_id'],)).fetchone()
        _require(row is not None)
        value = dict(row)
    ref = value.get('source_ref') or ''
    if not ref.startswith('parent_approved_supply:'):
        # The published source prefix is checked below against the exact package.
        if not ref.startswith('parent_approved_') or ':' not in ref:
            _require(not declared)
            return None
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
        _require(not declared)
        return None
    row = c.execute('''SELECT p.payload_json,p.source_snapshot_json FROM curriculum_supply_package p
        JOIN specialized_supply_candidate sc ON sc.candidate_id=p.candidate_id
        WHERE p.candidate_id=? AND sc.diagnostic_item_id=?''',
        (ref.partition(':')[2], value.get('diagnostic_item_id'))).fetchone()
    if not row:
        _require(not declared)
        return None
    payload, proof = json.loads(row[0]), json.loads(row[1])
    _require(isinstance(payload, dict) and isinstance(proof, dict))
    original = payload.get('open_response')
    if not isinstance(original, dict) or original.get('schema') != 'reviewed_open_response_source_v3':
        _require(not declared)
        return None
    _require(all('child_id' in {r[1] for r in c.execute('PRAGMA table_info('+table+')')}
        for table in ('curriculum_supply_package','specialized_supply_candidate')))
    _require(c.execute('''SELECT 1 FROM curriculum_supply_package p JOIN specialized_supply_candidate sc
        ON sc.candidate_id=p.candidate_id AND sc.child_id=p.child_id
        WHERE p.candidate_id=? AND sc.diagnostic_item_id=?''',
        (ref.partition(':')[2], value.get('diagnostic_item_id'))).fetchone() is not None)
    _require(all(requested[k] == value.get(k) for k in
        ('prompt', 'stimulus_text', 'stimulus_asset', 'source_ref', 'expected_answer_json') if k in requested))
    from .open_response_source import parse
    original = parse(json.dumps(original))
    source = proof.get('source')
    _require(isinstance(source, dict) and isinstance(source.get('content_text'), str))
    _require(source.get('excerpt_hash') == hashlib.sha256(source['content_text'].encode()).hexdigest()
        and parse(source['content_text']) == original and value['prompt'] == original['prompt'])
    saved = proof.get('open_response_materials')
    _require(isinstance(saved, dict) and isinstance(saved.get('materials'), list)
        and len(saved['materials']) == len(original['material_bindings']))
    visible, materials = [], []
    for binding, material in zip(original['material_bindings'], saved['materials']):
        _require(isinstance(material, dict) and material.get('binding') == binding)
        text, visual = material.get('text'), material.get('visual')
        _require(isinstance(text, str) and isinstance(visual, dict)
            and visual.get('binding') == binding['visual'])
        excerpt = material.get('excerpt_basis')
        text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        _require(isinstance(excerpt, dict)
            and text_hash == binding['text_hash'] == binding['excerpt_hash'] == excerpt.get('content_hash')
            and excerpt.get('excerpt_id') == binding['excerpt_id'] and excerpt.get('source_id') == binding['source_id']
            and binding['span_start'] == 0 and binding['span_end'] == len(text))
        descriptor = visual.get('image_descriptor')
        region = visual.get('region')
        _require(isinstance(descriptor, dict) and isinstance(region, dict))
        _require(set(descriptor) == {'kind','artifact_id','revision_id','region_id','region_version',
                                    'sha256','width','height','mime_type'}
            and descriptor['kind'] == 'bounded_diagram_region' and descriptor['mime_type'] == 'image/png'
            and all(descriptor[k] == binding['visual'][k] for k in ('artifact_id','revision_id','region_id','region_version')))
        sha = region.get('safe_png_sha256')
        _require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha)
            and descriptor.get('sha256') == sha
            and all(type(region.get(k)) is int and region[k] > 0 and descriptor.get(k) == region[k]
                    for k in ('width', 'height')))
        visible.append(dict(text=' '.join(text.split()), sha256=sha,
                            width=region['width'], height=region['height']))
        materials.append(dict(material_key=binding['material_key'], binding=binding['visual'],
                              image_descriptor=descriptor))
    _require(saved.get('stimulus_text') == '\n\n'.join('['+m['material_key']+']\n'+m['text'] for m in saved['materials']))
    from .local_supply import digest
    _require(saved.get('binding_version') == digest({'version':'open_response_visual_materials_v3',
        'materials':saved['materials']}))
    return dict(visible=visible, materials=materials, source=original,
                candidate_id=ref.partition(':')[2], stimulus_text=saved['stimulus_text'])


def current_visual(c, child_id, item):
    frozen = frozen_visual(c, item)
    if frozen is None:
        return None
    from .local_supply import validate_item
    from .open_response_materials import resolve_source_materials
    from .visual_material_host import assert_host_visual_current
    validate_item(c, child_id, item['diagnostic_item_id'])
    resolve_source_materials(c, child_id=child_id, source=frozen['source'], candidate_id=frozen['candidate_id'])
    for material in frozen['materials']:
        assert_host_visual_current(c, child_id, material['binding'], require_managed=True)
    return dict(version=VERSION, status='current', source_current=True, image_availability='available',
        binding_version=_digest(frozen['materials']), materials=frozen['materials'],
        content_identity=_digest(frozen['visible']), reason=None, **FLAGS)


def validate_display_evidence(c, child_id, current, evidence):
    """The review request binds display facts to the immutable original assessment."""
    visual = current['material'].get('visual')
    if visual is None:
        _require(evidence is None, 'NOT_APPLICABLE')
        return
    _require(current['source_status'] == 'current' and visual['status'] == 'current')
    _require(isinstance(evidence, dict) and set(evidence) == {
        'assessment_id', 'assessment_fingerprint', 'binding_version', 'displays'}, 'DISPLAY_REQUIRED')
    _require(evidence['assessment_id'] == current['assessment_id']
        and evidence['assessment_fingerprint'] == current['fingerprint']
        and evidence['binding_version'] == visual['binding_version'], 'ASSESSMENT_CHANGED')
    assessment = c.execute('SELECT payload_json,fingerprint FROM rubric_assessment WHERE assessment_id=? AND child_id=?',
                          (current['assessment_id'], child_id)).fetchone()
    _require(assessment and assessment['fingerprint'] == current['fingerprint']
        and hashlib.sha256(assessment['payload_json'].encode()).hexdigest() == current['fingerprint'], 'ASSESSMENT_CHANGED')
    displays = evidence['displays']
    _require(isinstance(displays, list) and len(displays) == len(visual['materials']), 'DISPLAY_REQUIRED')
    from .visual_material_host import ACTION, VisualMaterialHost
    from .database import local_media_scope
    from .local_supply import digest
    scope = local_media_scope(c)
    _require(scope.get('media_root') is not None, 'MEDIA_RUNTIME_REQUIRED')
    host = VisualMaterialHost(None, data_dir=scope['media_root'], data_encryption_key=scope.get('data_encryption_key'))
    ids = set()
    for material, display in zip(visual['materials'], displays):
        _require(isinstance(display, dict) and set(display) == {'material_key', 'display_receipt_id'}
            and display['material_key'] == material['material_key']
            and isinstance(display['display_receipt_id'], str), 'DISPLAY_REQUIRED')
        receipt_id = display['display_receipt_id']
        _require(receipt_id not in ids, 'DISPLAY_REQUIRED')
        ids.add(receipt_id)
        row = c.execute('SELECT * FROM audit_event WHERE audit_id=? AND child_id=? AND actor=? AND action=?',
            (receipt_id, child_id, 'parent_ui', ACTION)).fetchone()
        _require(row is not None, 'DISPLAY_REQUIRED')
        data = json.loads(row['summary_json'])
        _require(isinstance(data, dict), 'DISPLAY_REQUIRED')
        request, result = data.get('request'), data.get('result')
        binding, descriptor = material['binding'], material['image_descriptor']
        _require(isinstance(request, dict) and isinstance(result, dict)
            and data.get('request_hash') == digest(request)
            and request.get('kind') == 'displayed' and result.get('status') == 'displayed'
            and data.get('artifact_id') == binding['artifact_id'] and data.get('region_id') == binding['region_id']
            and all(request.get(k) == binding[k] for k in ('revision_id', 'region_version', 'facts_version'))
            and all(request.get(k) == descriptor[k] for k in ('sha256', 'width', 'height'))
            and _display_time(row['occurred_at']) >= _display_time(current['created_at']), 'DISPLAY_CHANGED')
        _, grant = host._grant(c, child_id, binding['artifact_id'], binding['region_id'], binding['region_version'])
        _require(grant and result.get('grant_id') == grant['grant_id'], 'DISPLAY_CHANGED')
    for row in c.execute('SELECT assessment_id,payload_json FROM rubric_review_event WHERE child_id=?', (child_id,)):
        try:
            previous = json.loads(row['payload_json'])
            previous = previous.get('visual_evidence') if isinstance(previous, dict) else None
            used = previous.get('displays', []) if isinstance(previous, dict) else []
            _require(row['assessment_id'] == current['assessment_id'] or not any(
                isinstance(d, dict) and d.get('display_receipt_id') in ids for d in used), 'DISPLAY_ALREADY_USED')
        except (TypeError, json.JSONDecodeError):
            raise ValueError('RUBRIC_VISUAL_PRIOR_DISPLAY_UNAVAILABLE') from None

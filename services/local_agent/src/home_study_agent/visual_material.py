"""Local visual-source versions, not upload permission or scoring authority.

Writers borrow the host's transaction. Only the authenticated parent host may
call confirm_facts; provider output can only call record_facts. The original
frame and returned derivative bytes are transient and are never persisted here.
"""
from hashlib import sha256
import json
import re
from datetime import datetime, timezone

from .local_supply import LocalSupplyError, digest

VERSION = 'visual_material_v1'
SCHEMA = 'reviewed_open_response_source_v3'
PURPOSE = 'visual_material_formative_v3'
FLAGS = dict(local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)
BINDING_FIELDS = {'artifact_id', 'revision_id', 'region_id', 'region_version', 'facts_version', 'confirmation_id'}


def require(value, code):
    if not value:
        raise LocalSupplyError('VISUAL_MATERIAL_' + code)


def _json(value):
    def pairs(items):
        result = {}
        for k, v in items:
            require(k not in result, 'RECORD_INVALID')
            result[k] = v
        return result
    try:
        result = json.loads(value, object_pairs_hook=pairs)
    except (ValueError, TypeError) as error:
        raise LocalSupplyError('VISUAL_MATERIAL_RECORD_INVALID') from error
    require(isinstance(result, dict), 'RECORD_INVALID')
    return result


def validate_visual_binding(binding, *, confirmed=True):
    require(isinstance(binding, dict) and set(binding) == BINDING_FIELDS, 'BINDING_INVALID')
    for key in ('artifact_id', 'revision_id', 'region_id'):
        require(isinstance(binding[key], str) and 0 < len(binding[key]) <= 128, 'BINDING_INVALID')
    require(isinstance(binding['region_version'], str) and re.fullmatch('[0-9a-f]{64}', binding['region_version']), 'BINDING_INVALID')
    if confirmed:
        require(isinstance(binding['facts_version'], str) and re.fullmatch('[0-9a-f]{64}', binding['facts_version'])
                and isinstance(binding['confirmation_id'], str) and len(binding['confirmation_id']) <= 128
                and binding['confirmation_id'], 'CONFIRMATION_REQUIRED')


def validate_facts(facts):
    require(isinstance(facts, dict) and set(facts) == {'version', 'observations', 'uncertainties'}
            and facts['version'] == 'visual_facts_v1', 'FACTS_INVALID')
    require(isinstance(facts['observations'], list) and 1 <= len(facts['observations']) <= 64
            and isinstance(facts['uncertainties'], list) and len(facts['uncertainties']) <= 32, 'FACTS_INVALID')
    ids = set()
    for observation in facts['observations']:
        require(isinstance(observation, dict) and set(observation) == {'id', 'kind', 'description', 'bbox'}, 'FACTS_INVALID')
        require(isinstance(observation['id'], str) and re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,39}', observation['id'])
                and observation['id'] not in ids, 'FACTS_INVALID')
        ids.add(observation['id'])
        require(isinstance(observation['kind'],str) and observation['kind'] in {'shape', 'mark', 'label', 'connection', 'position', 'scale'}, 'FACTS_INVALID')
        bounds = observation['bbox']
        require(isinstance(bounds, list) and len(bounds) == 4 and all(type(v) in {int, float} and 0 <= v <= 1 for v in bounds)
                and bounds[0] < bounds[2] and bounds[1] < bounds[3], 'FACTS_INVALID')
        require(isinstance(observation['description'], str) and 0 < len(observation['description'].strip()) <= 600, 'FACTS_INVALID')
    require(all(isinstance(v, str) and 0 < len(v.strip()) <= 400 for v in facts['uncertainties']), 'FACTS_INVALID')
    return facts


def _page(c, scope, *, live=False):
    row = c.execute('''SELECT a.*,r.monitor_id,r.revision_index,r.content_sha256,r.content_signature,
        r.created_at AS revision_created_at,m.generation,m.status AS monitor_status,
        ds.status AS session_status,ds.state,cp.status AS child_status
        FROM capture_artifact a JOIN document_page_revision r
        ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
        JOIN document_monitor_session m ON m.monitor_id=r.monitor_id AND m.child_id=a.child_id AND m.session_id=a.session_id
        JOIN daily_session ds ON ds.session_id=a.session_id AND ds.child_id=a.child_id
        JOIN child_profile cp ON cp.child_id=a.child_id
        WHERE a.artifact_id=? AND r.revision_id=? AND a.child_id=? AND a.session_id=?''',
        (scope['artifact_id'], scope['revision_id'], scope['child_id'], scope['session_id'])).fetchone()
    require(row and row['artifact_type'] == 'document_ocr' and row['status'] in {'completed', 'quality_warning'}
            and row['child_status'] == 'active', 'SOURCE_CHANGED')
    from .document_monitoring import DocumentMonitoringService
    require(isinstance(row['extracted_text'], str) and sha256(DocumentMonitoringService._normalized_text(
        row['extracted_text']).encode()).hexdigest() == row['content_sha256'], 'PAGE_CHANGED')
    details = _json(row['details_json'])
    require(isinstance(details.get('monitor_frame_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', details['monitor_frame_sha256']), 'FRAME_PROOF_REQUIRED')
    if live:
        require(row['session_status'] == 'active' and row['state'] == 'study' and row['monitor_status'] == 'active', 'SESSION_NOT_ACTIVE')
        require(type(details.get('monitor_generation')) is int and details['monitor_generation'] == row['generation'], 'GENERATION_CHANGED')
        latest = c.execute('SELECT revision_id FROM document_page_revision WHERE monitor_id=? ORDER BY revision_index DESC LIMIT 1',
                           (row['monitor_id'],)).fetchone()
        require(latest and latest[0] == scope['revision_id'], 'PAGE_NOT_CURRENT')
    basis = {k: row[k] for k in ('artifact_id', 'child_id', 'session_id', 'artifact_type', 'extracted_text',
        'confidence', 'status', 'created_at', 'monitor_id', 'revision_index', 'content_sha256', 'content_signature', 'revision_created_at')}
    basis['revision_id'] = scope['revision_id']
    basis['details'] = {k: v for k, v in details.items() if k != 'visual_materials'}
    return row, details, digest(basis)


def _save(c, row, details):
    require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
    changed = c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=? AND child_id=? AND details_json=?',
        (json.dumps(details, ensure_ascii=False), row['artifact_id'], row['child_id'], row['details_json']))
    require(changed.rowcount == 1, 'CAS_CONFLICT')


def _binding(scope, region_id, region):
    return dict(artifact_id=scope['artifact_id'], revision_id=scope['revision_id'], region_id=region_id,
                region_version=region['region_version'], facts_version=region.get('facts_version'),
                confirmation_id=region.get('confirmation_id'))


def _image_descriptor(binding, region):
    return dict(kind='bounded_diagram_region', **{k:binding[k] for k in
        ('artifact_id','revision_id','region_id','region_version')}, sha256=region['safe_png_sha256'],
        width=region['width'], height=region['height'], mime_type='image/png')


def record_region(c, *, child_id, session_id, artifact_id, revision_id, prepared,
                  region_id, expected_details_version):
    """Short write only. Prepare pixels before BEGIN, then recheck live scope."""
    from .document_vision import is_prepared_diagram
    require(is_prepared_diagram(prepared), 'PREPARED_REQUIRED')
    require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
    require(isinstance(region_id, str) and re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,39}', region_id), 'REGION_INVALID')
    scope = dict(child_id=child_id, session_id=session_id, artifact_id=artifact_id, revision_id=revision_id)
    row, details, source_version = _page(c, scope, live=True)
    before_version = digest(details)
    require(prepared.frame_sha256 == details['monitor_frame_sha256'], 'FRAME_CHANGED')
    names = sorted(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
    require(prepared.redaction_scope_sha256 == sha256(json.dumps(names,ensure_ascii=False).encode()).hexdigest(), 'PRIVACY_SCOPE_CHANGED')
    region_xyxy = prepared.region_xyxy
    region = dict(version=VERSION, source_version=source_version, frame_sha256=details['monitor_frame_sha256'],
        region_xyxy=list(region_xyxy), width=prepared.width, height=prepared.height,
        frame_width=prepared.frame_width, frame_height=prepared.frame_height,
        coordinate_space='original_frame_pixels',
        crop_to_frame=[1,0,region_xyxy[0],0,1,region_xyxy[1],0,0,1],
        safe_png_sha256=sha256(prepared.png).hexdigest(), gate_version=prepared.gate_version,
        page_corners=[list(point) for point in prepared.corners], captured_generation=row['generation'], **FLAGS)
    region['region_version'] = digest(region)
    regions = details.setdefault('visual_materials', {})
    require(isinstance(regions, dict), 'RECORD_INVALID')
    if region_id in regions:
        require(isinstance(regions[region_id], dict) and regions[region_id].get('region_version') == region['region_version'], 'REGION_ID_CONFLICT')
        region = regions[region_id]
    else:
        require(before_version == expected_details_version, 'CAS_CONFLICT')
        regions[region_id] = region
        _save(c, row, details)
    binding = _binding(scope, region_id, region)
    return {**region, 'binding': binding, 'image_descriptor':_image_descriptor(binding,region), 'derived_png': prepared.png}


def _region(c, scope, region_id, region_version, *, live=False):
    row, details, source_version = _page(c, scope, live=live)
    regions = details.get('visual_materials')
    region = regions.get(region_id) if isinstance(regions, dict) else None
    require(isinstance(region, dict) and region.get('source_version') == source_version
            and region.get('region_version') == region_version, 'REGION_CHANGED')
    immutable = {k: v for k, v in region.items() if k not in {'region_version', 'facts', 'facts_version', 'confirmation_id', 'history', 'commands', 'review_status'}}
    require(digest(immutable) == region_version, 'REGION_CHANGED')
    if live:
        require(region.get('captured_generation') == row['generation'], 'GENERATION_CHANGED')
    return row, details, region


def _command(region, key, request):
    require(isinstance(key, str) and 0 < len(key) <= 128, 'COMMAND_REQUIRED')
    commands = region.get('commands', {})
    require(isinstance(commands, dict), 'RECORD_INVALID')
    prior = commands.get(key)
    if prior:
        require(isinstance(prior,dict) and set(prior)=={'request','result'} and isinstance(prior['result'],dict), 'RECORD_INVALID')
        require(prior['request'] == digest(request), 'REQUEST_CONFLICT')
    return prior


def record_facts(c, *, child_id, session_id, artifact_id, revision_id, region_id,
                 region_version, facts, expected_facts_version, client_action_id):
    scope = dict(child_id=child_id, session_id=session_id, artifact_id=artifact_id, revision_id=revision_id)
    require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
    row, details, region = _region(c, scope, region_id, region_version, live=True)
    validate_facts(facts)
    request = dict(kind='facts', facts=facts, expected=expected_facts_version, region_version=region_version)
    prior = _command(region, client_action_id, request)
    if prior:
        return prior['result']
    require(region.get('facts_version') == expected_facts_version, 'CAS_CONFLICT')
    if region.get('facts'):
        region.setdefault('history', []).append({k: region.get(k) for k in ('facts', 'facts_version', 'confirmation_id')})
    region.update(facts=facts, facts_version=digest({'region_version': region_version, 'facts': facts}), confirmation_id=None, review_status='unconfirmed')
    result = dict(facts_version=region['facts_version'], status='needs_confirmation', **FLAGS)
    region.setdefault('commands', {})[client_action_id] = dict(request=digest(request), result=result)
    _save(c, row, details)
    return result


def confirm_facts(c, *, child_id, session_id, artifact_id, revision_id, region_id,
                  region_version, facts_version, decision, client_action_id, _display_proof=None):
    """Parent-authenticated internal host hook. Never expose as provider input."""
    scope = dict(child_id=child_id, session_id=session_id, artifact_id=artifact_id, revision_id=revision_id)
    require(c.in_transaction and decision in {'confirm', 'retract'}, 'CONFIRMATION_INVALID')
    historical = False
    if _display_proof is not None:
        from .visual_material_host import valid_display_proof
        historical = valid_display_proof(_display_proof, c, scope, region_id, region_version, facts_version)
        require(historical, 'DISPLAY_REQUIRED')
    row, details, region = _region(c, scope, region_id, region_version, live=decision == 'confirm' and not historical)
    request = dict(kind=decision, facts_version=facts_version, region_version=region_version)
    prior = _command(region, client_action_id, request)
    if prior:
        current = _binding(scope, region_id, region)
        return {**prior['result'], 'status':prior['result']['status'] if current==prior['result']['binding'] else 'superseded'}
    require(region.get('facts_version') == facts_version
            and digest({'region_version': region_version, 'facts': validate_facts(region.get('facts'))}) == facts_version, 'FACTS_CHANGED')
    require(decision == 'retract' or not region['facts']['uncertainties'], 'UNCERTAINTY_REQUIRES_REVIEW')
    audit_id = 'visual:' + digest({'scope': scope, 'region_id': region_id, 'key': client_action_id})
    c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
        (audit_id, 'parent_ui', 'visual_material_confirmation_v1', child_id,
         json.dumps(dict(**scope, region_id=region_id, region_version=region_version, facts_version=facts_version, decision=decision)),
         datetime.now(timezone.utc).isoformat()))
    region['confirmation_id'] = audit_id if decision == 'confirm' else None
    region['review_status'] = 'confirmed' if decision == 'confirm' else 'retracted'
    result = dict(status='ready_local' if decision == 'confirm' else 'withdrawn', binding=_binding(scope, region_id, region), **FLAGS)
    region.setdefault('commands', {})[client_action_id] = dict(request=digest(request), result=result)
    _save(c, row, details)
    return result


def resolve_visual_material(c, *, child_id, binding):
    if not c.in_transaction:
        from .open_response_materials import _snapshot
        with _snapshot(c):
            return resolve_visual_material(c,child_id=child_id,binding=binding)
    validate_visual_binding(binding)
    row = c.execute('SELECT session_id FROM capture_artifact WHERE artifact_id=? AND child_id=?', (binding['artifact_id'], child_id)).fetchone()
    require(row, 'SOURCE_CHANGED')
    scope = dict(child_id=child_id, session_id=row['session_id'], artifact_id=binding['artifact_id'], revision_id=binding['revision_id'])
    _, _, region = _region(c, scope, binding['region_id'], binding['region_version'])
    require(region.get('review_status')=='confirmed' and _binding(scope, binding['region_id'], region) == binding, 'CONFIRMATION_CHANGED')
    facts = validate_facts(region.get('facts'))
    require(digest({'region_version': binding['region_version'], 'facts': facts}) == binding['facts_version'], 'FACTS_CHANGED')
    audit = c.execute('SELECT * FROM audit_event WHERE audit_id=? AND child_id=? AND actor=? AND action=?',
        (binding['confirmation_id'], child_id, 'parent_ui', 'visual_material_confirmation_v1')).fetchone()
    require(audit and _json(audit['summary_json']) == dict(**scope, region_id=binding['region_id'],
        region_version=binding['region_version'], facts_version=binding['facts_version'], decision='confirm'), 'CONFIRMATION_CHANGED')
    from .visual_material_host import assert_host_visual_current
    assert_host_visual_current(c, child_id, binding)
    return dict(status='ready_local', fact_status='confirmed', binding=binding, facts=facts,
        image_availability='host_unverified', parent_confirmation_ready=False,
        image_descriptor=_image_descriptor(binding,region), region={k: region[k] for k in
        ('source_version', 'frame_sha256', 'region_xyxy', 'safe_png_sha256', 'width', 'height', 'gate_version',
         'frame_width', 'frame_height', 'coordinate_space', 'crop_to_frame')}, **FLAGS)


def read_visual_candidate(c, *, child_id, artifact_id, revision_id, region_id, region_version):
    """Unconfirmed observations for local clarification, never scored facts.

    An authenticated live teaching host may ask the child to check uncertainty.
    Parent confirmation is not required for this candidate view. This grants
    neither cloud transmission nor calibrated visual/scoring qualification.
    """
    if not c.in_transaction:
        from .open_response_materials import _snapshot
        with _snapshot(c):
            return read_visual_candidate(c,child_id=child_id,artifact_id=artifact_id,revision_id=revision_id,
                region_id=region_id,region_version=region_version)
    row = c.execute('SELECT session_id FROM capture_artifact WHERE artifact_id=? AND child_id=?', (artifact_id, child_id)).fetchone()
    require(row, 'SOURCE_CHANGED')
    scope = dict(child_id=child_id, session_id=row['session_id'], artifact_id=artifact_id, revision_id=revision_id)
    _, _, region = _region(c, scope, region_id, region_version, live=True)
    require(region.get('review_status')!='retracted', 'FACTS_RETRACTED')
    facts = validate_facts(region.get('facts'))
    require(digest({'region_version':region_version,'facts':facts}) == region.get('facts_version'), 'FACTS_CHANGED')
    binding = _binding(scope,region_id,region)
    return dict(status='needs_clarification', fact_status=region.get('review_status','unconfirmed'),
        image_availability='host_unverified', parent_confirmation_ready=False,
        observations=facts, binding=binding, image_descriptor=_image_descriptor(binding,region),
        can_explain_uncertain=True, can_score=False, can_update_mastery=False,
        interpretation_notice='图形识别尚未核实，请先核对标记和关系；不能据此判断作答对错。', **FLAGS)


def visual_question_identity(prompt, visual):
    """No OCR-only equivalence for a question which needs a figure."""
    return digest({'version': VERSION, 'prompt': prompt, 'visual': visual.get('binding', visual)})

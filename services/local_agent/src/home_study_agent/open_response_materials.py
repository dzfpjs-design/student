"""Exact local text materials. Readiness is neither cloud permission nor a score."""
from contextlib import contextmanager
from hashlib import sha256
import json
import re

from .local_supply import LocalSupplyError, digest
from .family_clock import family_today

VERSION = 'open_response_materials_v1'
BINDING_FIELDS = {'material_key', 'source_id', 'source_version', 'excerpt_id', 'excerpt_hash',
                  'span_start', 'span_end', 'text_hash'}
SOURCE_FIELDS = ('source_id', 'child_id', 'source_type', 'title', 'subject', 'grade_band',
                 'version_label', 'license_status', 'review_status', 'effective_from', 'effective_to',
                 'content_hash', 'imported_at', 'publisher')
FLAGS = dict(local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)
REVISION_FIELDS = {'current_version', 'predecessor_id'}


def require(value, code):
    if not value:
        raise LocalSupplyError('OPEN_RESPONSE_MATERIAL_' + code)


@contextmanager
def _snapshot(c):
    borrowed = c.in_transaction
    changes = c.total_changes if not borrowed else None
    if not borrowed:
        c.execute('BEGIN')
    try:
        yield
    finally:
        if not borrowed:
            if c.total_changes != changes:
                c.rollback()
                raise LocalSupplyError('OPEN_RESPONSE_MATERIAL_READ_SNAPSHOT_MUTATED')
            # A pure read has nothing to undo. Python 3.9 rollback invalidates
            # outer active cursors even when the material is only unavailable.
            c.commit()


def validate_bindings(bindings):
    require(isinstance(bindings, list) and 1 <= len(bindings) <= 8, 'BINDING_REQUIRED')
    keys, excerpts = set(), set()
    for binding in bindings:
        require(isinstance(binding, dict) and set(binding) == BINDING_FIELDS, 'BINDING_INVALID')
        key = binding['material_key']
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', key), 'KEY_INVALID')
        for field in ('source_id', 'excerpt_id'):
            require(isinstance(binding[field], str) and 0 < len(binding[field]) <= 200, 'BINDING_INVALID')
        for field in ('source_version', 'excerpt_hash', 'text_hash'):
            require(isinstance(binding[field], str) and re.fullmatch(r'[0-9a-f]{64}', binding[field]), 'HASH_INVALID')
        require(type(binding['span_start']) is int and type(binding['span_end']) is int
                and binding['span_start'] == 0 and 0 < binding['span_end'] <= 1000, 'COMPLETE_EXCERPT_REQUIRED')
        require(key not in keys and binding['excerpt_id'] not in excerpts, 'DUPLICATE')
        keys.add(key)
        excerpts.add(binding['excerpt_id'])


def validate_visual_bindings(bindings):
    from .visual_material import validate_visual_binding
    require(isinstance(bindings, list) and 1 <= len(bindings) <= 8, 'BINDING_REQUIRED')
    require(all(isinstance(b, dict) and set(b) == BINDING_FIELDS | {'visual'} for b in bindings), 'BINDING_INVALID')
    validate_bindings([{k: v for k, v in b.items() if k != 'visual'} for b in bindings])
    identities = set()
    for binding in bindings:
        validate_visual_binding(binding['visual'])
        identity = digest(binding['visual'])
        require(identity not in identities, 'DUPLICATE')
        identities.add(identity)


def _read_excerpt(c, child_id, excerpt_id):
    child = c.execute('SELECT grade,status FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
    require(child and child['status'] == 'active', 'CHILD_UNAVAILABLE')
    excerpt = c.execute('SELECT * FROM knowledge_excerpt WHERE excerpt_id=?', (excerpt_id,)).fetchone()
    require(excerpt is not None, 'SOURCE_UNAVAILABLE')
    source = c.execute('SELECT * FROM knowledge_source WHERE source_id=? AND child_id=?',
                       (excerpt['source_id'], child_id)).fetchone()
    require(source is not None, 'SOURCE_UNAVAILABLE')
    source, excerpt = dict(source), dict(excerpt)
    require(source['source_type'] in {'school_material', 'curriculum'}
            and source['review_status'] == 'approved'
            and source.get('current_version', 1) == 1
            and source['license_status'] in {'public', 'family_owned_index'}
            and source['grade_band'] == child['grade'], 'SOURCE_NOT_REVIEWED')
    today = family_today().isoformat()
    require((not source['effective_from'] or source['effective_from'] <= today)
            and (not source['effective_to'] or source['effective_to'] >= today), 'SOURCE_EXPIRED')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_source_erasure'").fetchone():
        require(not c.execute('SELECT 1 FROM privacy_source_erasure WHERE child_id=? AND source_id=?',
                              (child_id, source['source_id'])).fetchone(), 'SOURCE_ERASED')
    text = excerpt['content_text']
    require(isinstance(text, str) and 0 < len(text) <= 1000
            and sha256(text.encode('utf-8')).hexdigest() == excerpt['content_hash'], 'EXCERPT_CHANGED_OR_TRUNCATED')
    require(not any(ord(char) < 32 and char not in '\n\r\t' for char in text), 'TEXT_INVALID')
    return source, excerpt


def _binding(source, excerpt, material_key):
    return dict(material_key=material_key, source_id=source['source_id'], source_version=digest(source),
        excerpt_id=excerpt['excerpt_id'], excerpt_hash=excerpt['content_hash'], span_start=0,
        span_end=len(excerpt['content_text']), text_hash=sha256(excerpt['content_text'].encode('utf-8')).hexdigest())


def bind_material_excerpt(c, *, child_id, excerpt_id, material_key):
    """Authoring descriptor from a whole stored paragraph; creates no grant or row."""
    with _snapshot(c):
        source, excerpt = _read_excerpt(c, child_id, excerpt_id)
        binding = _binding(source, excerpt, material_key)
        validate_bindings([binding])
        return binding


def _stored_proof(c, child_id, source, candidate_id):
    if candidate_id is None:
        return None
    row = c.execute('''SELECT payload_json,source_snapshot_json FROM curriculum_supply_package
        WHERE candidate_id=? AND child_id=? AND state IN ('draft','blocked','validated','reviewed','published')''',
        (candidate_id, child_id)).fetchone()
    if row is None and c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone():
        row = c.execute('''SELECT payload_json,source_snapshot_json FROM curriculum_first_source
            WHERE candidate_id=? AND child_id=? AND state IN ('draft','blocked','validated','reviewed','published')''',
            (candidate_id,child_id)).fetchone()
    require(row is not None, 'PUBLICATION_CHANGED')
    try:
        payload, proof = json.loads(row['payload_json']), json.loads(row['source_snapshot_json'])
    except (TypeError, ValueError) as error:
        raise LocalSupplyError('OPEN_RESPONSE_MATERIAL_PUBLICATION_CHANGED') from error
    require(isinstance(payload, dict) and payload.get('open_response') == source
            and isinstance(proof, dict), 'PUBLICATION_CHANGED')
    if not proof:
        return None
    require(isinstance(proof.get('source'), dict), 'PUBLICATION_CHANGED')
    return proof


def resolve_source_materials(c, *, child_id, source, candidate_id=None):
    """Publication validator's borrowed-connection hook; raises LocalSupplyError."""
    from .open_response_source import MATERIAL_SCHEMA, VISUAL_SCHEMA
    if source.get('schema') == VISUAL_SCHEMA:
        return _resolve_visual_source(c, child_id=child_id, source=source, candidate_id=candidate_id)
    require(source.get('schema') == MATERIAL_SCHEMA and source.get('context_requirement') == 'external_material', 'BINDING_REQUIRED')
    if source.get('material_semantics') != 'text_only':
        raise LocalSupplyError('OPEN_RESPONSE_IMAGE_SEMANTICS_REQUIRED')
    validate_bindings(source.get('material_bindings'))
    with _snapshot(c):
        proof = _stored_proof(c, child_id, source, candidate_id)
        saved = proof.get('open_response_materials') if proof else None
        if proof:
            require(isinstance(saved, dict) and isinstance(saved.get('materials'), list)
                    and len(saved['materials']) == len(source['material_bindings']), 'PUBLICATION_CHANGED')
        materials = []
        for index, binding in enumerate(source['material_bindings']):
            original, excerpt = _read_excerpt(c, child_id, binding['excerpt_id'])
            require(original['subject'] == source['subject'] and original['grade_band'] == source['grade'], 'SCOPE_CHANGED')
            frozen = saved['materials'][index] if saved else None
            if saved is not None:
                require(isinstance(frozen, dict) and frozen.get('binding') == binding
                        and isinstance(frozen.get('source_basis'), dict), 'PUBLICATION_CHANGED')
            exact = _binding(original, excerpt, binding['material_key']) == binding
            if not exact:
                # Only an existing pre-71 publication can retain its old hash.
                # New authoring cannot request this by supplying an old-shaped digest.
                require(proof is not None and not REVISION_FIELDS.intersection(proof['source'])
                        and not REVISION_FIELDS.intersection(frozen['source_basis'])
                        and REVISION_FIELDS.issubset(original)
                        and original['current_version'] == 1 and original['predecessor_id'] is None,
                        'VERSION_CHANGED')
                legacy = {k: v for k, v in original.items() if k not in REVISION_FIELDS}
                require(_binding(legacy, excerpt, binding['material_key']) == binding, 'VERSION_CHANGED')
            basis = {key: original[key] for key in SOURCE_FIELDS}
            basis.update({key: original[key] for key in REVISION_FIELDS if key in original})
            if frozen is not None and not REVISION_FIELDS.intersection(frozen['source_basis']):
                # Keep the original proof shape, including post-71 publications
                # made before these two fields were exposed in source_basis.
                basis = {k: v for k, v in basis.items() if k not in REVISION_FIELDS}
            materials.append(dict(material_key=binding['material_key'], binding=dict(binding), text=excerpt['content_text'],
                source_basis=basis,
                excerpt_basis={key: excerpt[key] for key in ('excerpt_id', 'source_id', 'sequence', 'heading', 'content_hash')}))
        stimulus = '\n\n'.join('[' + m['material_key'] + ']\n' + m['text'] for m in materials)
        audit = dict(version=VERSION, material_bindings=source['material_bindings'],
            source_basis=[m['source_basis'] for m in materials], excerpt_basis=[m['excerpt_basis'] for m in materials], **FLAGS)
        result = dict(binding_version=digest(dict(version=VERSION, materials=materials)), materials=materials,
            stimulus_text=stimulus, local_audit=audit, **FLAGS)
        require(saved is None or result == saved, 'PUBLICATION_CHANGED')
        return result


def _resolve_visual_source(c, *, child_id, source, candidate_id=None):
    from .visual_material import resolve_visual_material
    require(source.get('context_requirement') == 'external_material' and source.get('material_semantics') == 'image_required', 'BINDING_REQUIRED')
    validate_visual_bindings(source.get('material_bindings'))
    with _snapshot(c):
        proof = _stored_proof(c, child_id, source, candidate_id)
        saved = proof.get('open_response_materials') if proof else None
        materials, visuals = [], []
        for binding in source['material_bindings']:
            original, excerpt = _read_excerpt(c, child_id, binding['excerpt_id'])
            require(original['subject'] == source['subject'] and original['grade_band'] == source['grade'], 'SCOPE_CHANGED')
            require(_binding(original, excerpt, binding['material_key']) == {k: v for k, v in binding.items() if k != 'visual'}, 'VERSION_CHANGED')
            visual = resolve_visual_material(c, child_id=child_id, binding=binding['visual'])
            visuals.append(visual)
            materials.append(dict(material_key=binding['material_key'], binding=binding, text=excerpt['content_text'],
                source_basis={k: original[k] for k in (*SOURCE_FIELDS, *sorted(REVISION_FIELDS)) if k in original},
                excerpt_basis={k: excerpt[k] for k in ('excerpt_id','source_id','sequence','heading','content_hash')}, visual=visual))
        stimulus = '\n\n'.join('[' + m['material_key'] + ']\n' + m['text'] for m in materials)
        result = dict(binding_version=digest({'version':'open_response_visual_materials_v3','materials':materials}),
            materials=materials, visual_materials=visuals, stimulus_text=stimulus,
            local_audit=dict(version='open_response_visual_materials_v3', materials=materials, **FLAGS), **FLAGS)
        require(proof is None or saved == result, 'PUBLICATION_CHANGED')
        return result


def parent_source_materials(c, *, child_id, source, candidate_id=None):
    """Current parent-only readable projection; never expose paths or stale text."""
    base = dict(status='blocked', reason=None, materials=[], **FLAGS)
    try:
        resolved = resolve_source_materials(c, child_id=child_id, source=source, candidate_id=candidate_id)
        materials = [dict(material_key=m['material_key'], title=m['source_basis']['title'],
                          version_label=m['source_basis']['version_label'], heading=m['excerpt_basis']['heading'],
                          text=m['text']) for m in resolved['materials']]
        return {**base, 'status': 'ready', 'materials': materials}
    except LocalSupplyError as error:
        return {**base, 'reason': str(error)}


def resolve_open_response_materials(c, *, child_id, diagnostic_item_id, expected_item_version):
    """Read exact published item. A blocked result never includes material text."""
    from .local_supply import validate_item
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .open_response_source import candidate_source
    base = dict(status='blocked', reason=None, binding_version=None, materials=[], stimulus_text=None, local_audit=None, **FLAGS)
    try:
        with _snapshot(c):
            item = validate_item(c, child_id, diagnostic_item_id)
            require(_version(DiagnosticPromptService._item(c, child_id, diagnostic_item_id)) == expected_item_version, 'ITEM_VERSION_CHANGED')
            candidate = c.execute('SELECT * FROM specialized_supply_candidate WHERE diagnostic_item_id=? AND child_id=?',
                                  (diagnostic_item_id, child_id)).fetchone()
            if item['source_ref'].startswith('parent_approved_first:'):
                # validate_item above has replayed the first-root source and review.
                candidate = c.execute("SELECT * FROM curriculum_first_source WHERE diagnostic_item_id=? AND child_id=? AND state='published'",
                    (diagnostic_item_id,child_id)).fetchone()
                require(candidate is not None,'BINDING_REQUIRED')
                from .open_response_source import parse, VALIDATOR
                payload = json.loads(candidate['payload_json'])
                require(payload.get('validator')==VALIDATOR,'BINDING_REQUIRED')
                source = parse(json.dumps(payload.get('open_response')))
            else:
                require(candidate is not None, 'BINDING_REQUIRED')
                source = candidate_source(c, candidate)
            require(source is not None, 'BINDING_REQUIRED')
            result = resolve_source_materials(c, child_id=child_id, source=source, candidate_id=candidate['candidate_id'])
            return {**base, **result, 'status': 'ready'}
    except LocalSupplyError as error:
        return {**base, 'reason': str(error)}

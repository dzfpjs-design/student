"""Frozen shown-question identity, never current permission or scoring authority."""
import hashlib
import json

VERSION = 'diagnostic_history_identity_v1'
COLUMN = 'identity_snapshot_json'


class HistoryIdentityUnavailable(ValueError):
    pass


def _require(value):
    if not value:
        raise HistoryIdentityUnavailable('SCHOOL_REVIEW_HISTORY_UNAVAILABLE')


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def ensure_schema(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return
    if COLUMN not in {r[1] for r in c.execute('PRAGMA table_info(diagnostic_prompt)')}:
        c.execute('ALTER TABLE diagnostic_prompt ADD COLUMN identity_snapshot_json TEXT')
    # No second store: normal row export/delete/backup includes this evidence.
    # The existing source-erasure update also erases its new full-text copy.
    c.execute('''CREATE TRIGGER IF NOT EXISTS diagnostic_identity_immutable
        BEFORE UPDATE OF identity_snapshot_json ON diagnostic_prompt
        WHEN NEW.identity_snapshot_json IS NOT OLD.identity_snapshot_json
          AND NOT (NEW.identity_snapshot_json IS NULL AND NEW.status='invalidated'
                   AND NEW.reason='source_deleted' AND NEW.item_json='{}')
        BEGIN SELECT RAISE(ABORT,'DIAGNOSTIC_IDENTITY_IMMUTABLE'); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS diagnostic_identity_source_erased
        AFTER UPDATE OF item_json,status,reason ON diagnostic_prompt
        WHEN NEW.status='invalidated' AND NEW.reason='source_deleted'
          AND NEW.item_json='{}' AND NEW.identity_snapshot_json IS NOT NULL
        BEGIN UPDATE diagnostic_prompt SET identity_snapshot_json=NULL
              WHERE prompt_id=NEW.prompt_id; END''')


def snapshot(c, *, child_id, session_id, prompt_id, item):
    from .diagnostic_prompt import _version
    from .visual_question_evidence import frozen_visual
    visual = frozen_visual(c, item)
    proof = dict(version=VERSION, child_id=child_id, session_id=session_id,
        prompt_id=prompt_id, item_version=_version(item), item=dict(item),
        visual=visual)
    return _json(dict(proof=proof, sha256=_hash(proof)))


def _raw_item(c, item_id):
    row = c.execute('''SELECT di.*,kn.track,kn.subject,kn.title,kn.grade_band,kn.knowledge_version,
        kn.source_ref AS node_source_ref FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?''', (item_id,)).fetchone()
    return dict(row) if row else None


def _identity(item, visual):
    fields = {}
    for key in ('prompt', 'stimulus_text', 'stimulus_asset'):
        text = item.get(key) or ''
        _require(isinstance(text, str))
        fields[key] = text if key == 'stimulus_asset' else ' '.join(text.split())
    _require(fields['prompt'])
    if visual is not None:
        _require(isinstance(visual, dict) and visual.get('visible') and visual.get('materials'))
        _require(item['source_ref'].partition(':')[2] == visual['candidate_id'])
        _require(visual['source']['prompt'] == item['prompt'])
        _require(len(visual['visible']) == len(visual['materials']) == len(visual['source']['material_bindings']))
        for shown, material, binding in zip(visual['visible'], visual['materials'], visual['source']['material_bindings']):
            descriptor = material['image_descriptor']
            _require(material['material_key'] == binding['material_key'] and material['binding'] == binding['visual'])
            _require(all(descriptor[k] == material['binding'][k] for k in
                ('artifact_id', 'revision_id', 'region_id', 'region_version')))
            _require(all(shown[k] == descriptor[k] for k in ('sha256', 'width', 'height')))
        fields = dict(prompt=fields['prompt'], visual_content=_hash(visual['visible']))
        return 'exact_visual_question_v1:' + _hash(fields)
    return 'exact_question_v1:' + _hash(fields)


def _legacy_identity(c, child_id, item):
    from .learning_obligations import question_identity
    from .visual_question_evidence import frozen_visual
    visual = frozen_visual(c, item)
    if visual is not None:
        # A full item seal includes the original material/region versions, not
        # necessarily PNG bytes. Verify that exact retained region preimage too.
        from .visual_material import _region
        expected = json.loads(item['expected_answer_json'])
        _require(expected['material_bindings'] == visual['source']['material_bindings'])
        for material, shown in zip(visual['materials'], visual['visible']):
            binding = material['binding']
            artifact = c.execute('SELECT session_id FROM capture_artifact WHERE artifact_id=? AND child_id=?',
                (binding['artifact_id'], child_id)).fetchone()
            _require(artifact is not None)
            scope = dict(child_id=child_id, session_id=artifact['session_id'],
                artifact_id=binding['artifact_id'], revision_id=binding['revision_id'])
            _, _, region = _region(c, scope, binding['region_id'], binding['region_version'])
            _require(region['safe_png_sha256'] == shown['sha256']
                and region['width'] == shown['width'] and region['height'] == shown['height'])
        return _identity(item, visual)
    return question_identity(item, connection=c)


def historical_identity(c, child_id, prompt, target_identity):
    """Prove original identity without asking if that question is usable today.

    Damaged public snapshots retain the old non-overlap-only fallback, but only
    while the raw complete current row still verifies the original full seal.
    """
    from .diagnostic_prompt import _ITEM_FIELDS, _version
    try:
        _require(prompt['child_id'] == child_id)
        _require(prompt['reason'] != 'source_deleted')
        raw = _raw_item(c, prompt['diagnostic_item_id'])
        encoded = dict(prompt).get(COLUMN)
        if encoded is not None:
            envelope = json.loads(encoded)
            _require(isinstance(envelope, dict) and set(envelope) == {'proof', 'sha256'})
            proof = envelope['proof']
            _require(isinstance(proof, dict) and envelope['sha256'] == _hash(proof))
            _require(proof['version'] == VERSION and proof['child_id'] == child_id
                and proof['session_id'] == prompt['origin_session_id']
                and proof['prompt_id'] == prompt['prompt_id']
                and proof['item_version'] == prompt['item_version'])
            item = proof['item']
            _require(isinstance(item, dict) and item['diagnostic_item_id'] == prompt['diagnostic_item_id']
                and _version(item) == prompt['item_version'])
            expected = json.loads(item['expected_answer_json'])
            _require((isinstance(expected, dict) and expected.get('material_schema') == 'reviewed_open_response_source_v3')
                == (proof['visual'] is not None))
            identity = _identity(item, proof['visual'])
        else:
            _require(raw is not None and _version(raw) == prompt['item_version'])
            item = raw
            identity = _legacy_identity(c, child_id, raw)
        try:
            public = json.loads(prompt['item_json'])
        except (TypeError, ValueError):
            public = None
        if public != {key: item[key] for key in _ITEM_FIELDS}:
            _require(raw is not None and _version(raw) == prompt['item_version']
                and identity != target_identity)
        return identity
    except (ValueError, TypeError, KeyError) as error:
        raise HistoryIdentityUnavailable('SCHOOL_REVIEW_HISTORY_UNAVAILABLE') from error

"""Bind real evaluator executions to the existing calibration protocol.

No score/answer facts are copied here. Qualification is always read through the
core; a missing issuer, scope or certificate leaves normal teaching uncalibrated.
"""
from datetime import datetime, timezone
import json

from .rubric_calibration import RubricCalibrationService
from .rubric_calibration_evidence import digest, source_snapshot, require
from .rubric_calibration_evidence import timestamp
from .rubric_calibration_transaction import calibration_connection

VERSION = 'rubric_calibration_execution_v1'
LOCAL = {'local_only': True, 'cloud_transmission_allowed': False}


def describe_execution(evaluator, child_id, turn_id):
    from .learning_outcome_evaluator import response_contract,PROMPT_VERSION
    from .formative_criterion_review import VERSION as formative_version
    from .model_gateway import ModelGateway, ModelEndpoint, _scorer_request_provenance
    record = evaluator._load(turn_id)
    if record['child_id'] != child_id:
        raise KeyError(turn_id)
    with evaluator.database.connect() as c:
        c.execute('BEGIN')
        saved=c.execute('SELECT record_json FROM learning_evaluation_execution WHERE step_id=?',
            (record['step_id'],)).fetchone()
        if saved:
            frozen=json.loads(saved[0])
            require(isinstance(frozen,dict) and all(frozen.get(key)==record[key] for key in
                ('step_id','child_id','session_id','objective_id','source_evidence_id')), 'EXECUTION_BINDING_CHANGED')
            record=frozen
        projection = evaluator._assert_cloud_transmission(c, record)
        if not saved and projection is not None:
            step=c.execute('SELECT status,result_json FROM agent_step WHERE step_id=?',
                (record['step_id'],)).fetchone()
            # Completed pre-journal results stay historical; only a fresh claim
            # will choose today's response protocol.
            if not (step['status']=='completed' and step['result_json']):
                record={**record,'formative_protocol_version':formative_version}
        system_prompt,parser_version=response_contract(record,formative=projection is not None)
        source = source_snapshot(c, child_id, record['source_evidence_id'])
        profile, version, definitions = _profile(c, record)
    settings = evaluator.settings
    endpoint = ModelEndpoint(settings.model_provider, None, settings.model_base_url, settings.model_name)
    messages = [{'role': 'system', 'content': system_prompt}]
    actual = _scorer_request_provenance(endpoint, ModelGateway.request_payload(endpoint, messages, 'learning_outcome_evaluation'))
    return {'source': source, 'scorer': {'provider': settings.model_provider, 'model': settings.model_name,
        'model_revision': None, 'revision_receipt_id': None, 'profile_id': profile, 'profile_version': version,
        'prompt_hash': actual['system_prompt_sha256'], 'parameters_hash': actual['parameters_sha256'],
        'rubric_hash': digest(definitions), 'parser_version': parser_version,
        'projection_version': 'formative_evaluation_projection_v1' if projection else PROMPT_VERSION,
        'modality': source['modality']},
        'endpoint_scope_sha256': actual['endpoint_scope_sha256'], 'score_qualified': False,
        'missing': ['immutable_revision_proof', 'representative_holdout', 'independent_blind_labels'], **LOCAL}


def _profile(c, record):
    from .learning_rubric import open_profile_for_evidence, profile_for, rubric_dimensions, RUBRIC_VERSION
    profile = open_profile_for_evidence(c, record['child_id'], record.get('source_evidence_id'))
    return (profile['profile_id'] if profile else profile_for(record['subject']), RUBRIC_VERSION,
            profile['dimensions'] if profile else rubric_dimensions(record['subject']))


def _stratum(c, service, row, source, profile):
    manifest, scope = json.loads(row['manifest_json']), json.loads(row['scope_json'])
    frozen = [v for v in manifest['cases'] if v['evidence_id'] == source['evidence_id']]
    if frozen:
        return frozen[0]['stratum'] if len(frozen) == 1 and frozen[0]['source_version'] == source['version'] else None
    holdout = service._resolve(c, row['child_id'], 'holdout', manifest['holdout_receipt_id']) or {}
    item = c.execute('SELECT answer_type FROM diagnostic_item WHERE diagnostic_item_id=?',
                     (source['diagnostic_item_id'],)).fetchone()
    if not item or scope['task_family'] not in {item[0], profile}:
        return None
    # Membership is a signed, bounded rule over exact source metadata, not an
    # LLM guess or a caller-selected stratum. Legacy unspecified scopes abstain.
    rules = holdout.get('application_scope', [])
    matches = [r['stratum'] for r in rules if isinstance(r, dict)
               and set(r) == {'profile_id', 'answer_types', 'stratum'}
               and r['profile_id'] == profile and isinstance(r['answer_types'], list)
               and item[0] in r['answer_types'] and r['stratum'] in scope['strata']]
    return matches[0] if len(matches) == 1 else None


def prepare(c, record, messages, settings):
    from .learning_outcome_evaluator import PROMPT_VERSION,response_contract
    unknown = {'status': 'unknown', 'reason': 'no_current_calibration', **LOCAL}
    if not record.get('source_evidence_id') or not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rubric_calibration_protocol'").fetchone():
        return unknown
    try:
        source = source_snapshot(c, record['child_id'], record['source_evidence_id'])
        profile, version, definitions = _profile(c, record)
    except (ValueError, KeyError, TypeError):
        return unknown
    service = RubricCalibrationService(None)
    now = datetime.now(timezone.utc)
    projection_version = ('formative_evaluation_projection_v1' if record.get('formative_projection') else PROMPT_VERSION)
    _,parser_version=response_contract(record,formative=bool(record.get('formative_projection')))
    expected = {'provider': settings.model_provider, 'model': settings.model_name,
                'profile_id': profile, 'profile_version': version, 'rubric_hash': digest(definitions),
                'modality': source['modality'], 'parser_version': parser_version,
                'projection_version': projection_version,
                'prompt_hash': digest([m['content'] for m in messages if m['role'] == 'system'])}
    matches = []
    for row in c.execute('SELECT * FROM rubric_calibration_protocol WHERE child_id=? ORDER BY rowid', (record['child_id'],)):
        try:
            scorer, scope, manifest = [json.loads(row[k]) for k in ('scorer_json', 'scope_json', 'manifest_json')]
            if any(scorer.get(k) != v for k, v in expected.items()):
                continue
            if scope['subject'] != source['subject'] or scope['grade'] != source['grade']:
                continue
            current = service._read(c, row, now, _require_live=True)
            if current['status'] in {'revoked', 'invalidated'}:
                continue
            held_out = any(v['evidence_id'] == source['evidence_id'] for v in manifest['cases'])
            if not held_out and not current['score_qualified']:
                continue
            stratum = _stratum(c, service, row, source, profile)
            if stratum is None:
                continue
            score_binding = (None if held_out else service._score_binding(c, row,
                source['evidence_id'], now, current))
            matches.append({'version': VERSION, 'status': 'bound', 'mode': 'holdout' if held_out else 'score',
                'protocol_id': row['protocol_id'], 'protocol_version': digest(dict(row)), 'stratum': stratum,
                'child_id': record['child_id'], 'session_id': record['session_id'], 'source': source,
                'assessment_binding': {'protocol_id': row['protocol_id'], 'protocol_hash': row['protocol_hash'],
                    'scorer_hash': digest(scorer), 'source_version': source['version']},
                'score_binding': score_binding, **LOCAL})
        except (ValueError, KeyError, TypeError):
            continue
    return matches[0] if len(matches) == 1 else {**unknown, 'reason': 'ambiguous_calibration' if matches else unknown['reason']}


def revalidate(c, binding, *, phase='dispatch', assessment_id=None, _allow_abstention=False):
    if not binding or binding.get('status') != 'bound':
        return None
    require(binding.get('version') == VERSION, 'EXECUTION_BINDING_CHANGED')
    service, now = RubricCalibrationService(None), datetime.now(timezone.utc)
    row = service._owned(c, binding['child_id'], binding['protocol_id'])
    require(digest(dict(row)) == binding['protocol_version'], 'EXECUTION_BINDING_CHANGED')
    source = source_snapshot(c, binding['child_id'], binding['source']['evidence_id'])
    require(source == binding['source'] and source['session_id'] == binding['session_id'], 'ANSWER_SOURCE_CHANGED')
    view = service._read(c, row, now, _require_live=True)
    require(view['status'] not in {'revoked', 'invalidated'}, 'EXECUTION_AUTHORITY_CHANGED')
    scorer = json.loads(row['scorer_json'])
    require(_stratum(c, service, row, source, scorer['profile_id']) == binding['stratum'], 'EXECUTION_SCOPE_CHANGED')
    if binding['mode'] == 'score':
        service._revalidate_score_binding(c, row, binding['score_binding'],
                                         phase, now, assessment_id, view, _allow_abstention=_allow_abstention)
    else:
        require(binding['mode'] == 'holdout' and any(v['evidence_id'] == source['evidence_id']
            for v in json.loads(row['manifest_json'])['cases']), 'HOLDOUT_BINDING_CHANGED')
        if assessment_id:
            service._scored(c, row, assessment_id, now)
    accepted_at = datetime.now(timezone.utc)
    require(timestamp(row['created_at']) <= accepted_at < timestamp(row['expires_at']),
            'EXECUTION_AUTHORITY_CHANGED')
    return row


def actual_scorer_receipt(c, child_id, call_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'").fetchone():
        return None
    records = c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND model_call_id=?', (child_id, call_id)).fetchall()
    if len(records) != 1:
        return None
    try:
        payload = json.loads(records[0]['payload_json'])
        binding = payload.get('calibration_execution')
        if not binding or binding['child_id'] != child_id or digest(payload) != records[0]['fingerprint']:
            return None
        saved = c.execute('SELECT record_json FROM learning_evaluation_execution WHERE step_id=? AND model_call_id=?',
                          (records[0]['step_id'], call_id)).fetchone()
        if not saved or json.loads(saved[0]).get('calibration') != binding:
            return None
        row = RubricCalibrationService._owned(c, child_id, binding['protocol_id'])
        scorer, scope = json.loads(row['scorer_json']), json.loads(row['scope_json'])
        provenance = payload.get('scorer_provenance')
        if not isinstance(provenance, dict):
            return None
        return {'receipt_id': call_id, 'child_id': child_id, 'status': 'current',
                'scorer_hash': digest(scorer), 'protocol_hash': row['protocol_hash'],
                'scope_hash': digest(scope), 'stratum': binding['stratum'],
                'model_revision': scorer['model_revision'], 'provenance_hash': digest(provenance)}
    except (ValueError, KeyError, TypeError):
        return None


class DispatchGuard:
    def __init__(self, database, binding, original_guard):
        self.database, self.binding, self.original_guard = database, binding, original_guard

    def __call__(self):
        self.original_guard()
        with calibration_connection(self.database) as c:
            c.execute('BEGIN')
            revalidate(c, self.binding)

    def authorize_physical(self, provenance):
        if self.binding.get('status') != 'bound':
            return
        with calibration_connection(self.database) as c:
            c.execute('BEGIN')
            row = revalidate(c, self.binding)
            scorer = json.loads(row['scorer_json'])
            revision = RubricCalibrationService(None)._resolve(c, self.binding['child_id'], 'scorer_revision', scorer['revision_receipt_id'])
            require(all(provenance.get(k) == v for k, v in {
                'provider': scorer['provider'], 'requested_model': scorer['model'],
                'system_prompt_sha256': scorer['prompt_hash'], 'parameters_sha256': scorer['parameters_hash'],
                'endpoint_scope_sha256': revision['endpoint_scope_sha256']}.items()), 'PHYSICAL_SCORER_CHANGED')


def actual_audio_scorer_receipt(c,child_id,selector,*,projection_receipt_id,protocol_hash,as_of):
    """Exact local CPU authority; no model_call, feedback-call or provider alias."""
    from .rubric_audio_projection import project
    return project(c,child_id,selector,projection_receipt_id=projection_receipt_id,protocol_hash=protocol_hash,now=as_of)

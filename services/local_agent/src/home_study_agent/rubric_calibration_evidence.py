"""Read existing answer/score journals and compute calibration error bounds."""
from datetime import datetime, timezone
import hashlib
import json
import math


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(ok, code):
    if not ok:
        raise ValueError('RUBRIC_CALIBRATION_' + code)


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    require(isinstance(result, datetime) and result.tzinfo is not None, 'TIME_INVALID')
    return result.astimezone(timezone.utc)


def is_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(ch in '0123456789abcdef' for ch in value)


def source_snapshot(c, child_id, evidence_id):
    from .learning_obligations import withdrawn_evidence_ids
    from .local_supply import validate_item
    rows = c.execute('''SELECT da.* FROM diagnostic_attempt da JOIN learning_evidence le
        ON le.evidence_id=da.evidence_id AND le.child_id=da.child_id AND le.session_id=da.session_id
        JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
        WHERE da.child_id=? AND da.evidence_id=?''', (child_id, evidence_id)).fetchall()
    require(len(rows) == 1 and evidence_id not in withdrawn_evidence_ids(c, child_id), 'ANSWER_SOURCE_CHANGED')
    attempt = dict(rows[0])
    evidence = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                             (evidence_id, child_id)).fetchone())
    original = None
    modality = 'typed_text'
    from .writing_rubric import is_writing
    if is_writing(c, attempt['diagnostic_item_id']):
        from .writing_response_origin import read_original_written_source
        prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
                            (child_id, attempt['attempt_id'])).fetchall()
        require(len(prompts) == 1, 'EXACT_ORIGINAL_REQUIRED')
        checked = read_original_written_source(c, prompts[0], attempt)
        item, original = checked['item'], checked['original']
        modality = 'original_written_text'
    else:
        item = validate_item(c, child_id, attempt['diagnostic_item_id'])
    node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone())
    if item['answer_type'] == 'open_response':
        from .open_response_profile import profile_for_item
        if profile_for_item(item, node)['response_mode'] == 'spoken_explanation':
            from .school_spoken_score import original_response
            prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
                (child_id, attempt['attempt_id'])).fetchall()
            require(len(prompts) == 1, 'EXACT_ORIGINAL_REQUIRED')
            original = original_response(c, prompts[0], attempt)
            modality = 'original_spoken_text'
    return {'version': digest([attempt, evidence, item, node, original]),
            'diagnostic_item_id': item['diagnostic_item_id'], 'attempt_id': attempt['attempt_id'],
            'evidence_id': evidence_id, 'session_id': attempt['session_id'],
            'modality': modality,
            'subject': node['subject'], 'grade': c.execute('SELECT grade FROM child_profile WHERE child_id=?', (child_id,)).fetchone()[0]}


def assessment(c, child_id, assessment_id):
    from .learning_rubric import RubricEvidenceService
    row = c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND assessment_id=?',
                    (child_id, assessment_id)).fetchone()
    require(row is not None, 'ASSESSMENT_NOT_OWNED')
    current = RubricEvidenceService(None).get(child_id, assessment_id, connection=c)
    require(current['source_status'] == 'current' and current['source_evidence_id'], 'ASSESSMENT_SOURCE_CHANGED')
    require(digest(json.loads(row['payload_json'])) == row['fingerprint'], 'ASSESSMENT_HASH_CHANGED')
    source = source_snapshot(c, child_id, current['source_evidence_id'])
    call = c.execute('''SELECT * FROM model_call WHERE call_id=? AND child_id=? AND session_id=?
        AND purpose='learning_outcome_evaluation' ''', (row['model_call_id'], child_id, current['session_id'])).fetchone()
    require(call is not None and call['status'] == 'completed' and call['safety_status'] == 'passed'
            and call['verification_status'] in {'verified', 'partially_verified'}, 'MODEL_RECEIPT_REQUIRED')
    require(not current.get('review') or current['review']['operation'] != 'retract', 'ASSESSMENT_RETRACTED')
    return current, dict(call), source


def dimensions(current):
    keys = ('dimension_id', 'criterion', 'max_score', 'pass_score', 'score_anchors')
    values = current['dimensions']
    require(isinstance(values, list) and bool(values), 'DIMENSIONS_REQUIRED')
    result = [{key: d[key] for key in keys} for d in values]
    require(len({d['dimension_id'] for d in result}) == len(result)
            and all(d['max_score'] == 3 and d['pass_score'] == 2 for d in result), 'SCALE_NOT_SUPPORTED')
    return result


def score_values(current, values):
    definitions = dimensions(current)
    from .learning_rubric import RubricEvidenceService
    require(isinstance(values, list) and len(values) == len(definitions)
            and {d.get('dimension_id') for d in values} == {d['dimension_id'] for d in definitions}, 'DIMENSION_COVERAGE')
    result = {}
    for d in values:
        score = d.get('score')
        require(score is None or type(score) is int and 0 <= score <= 3, 'SCORE_INVALID')
        if score is not None:
            require(RubricEvidenceService._quotes(d.get('response_quotes'), current['response'], required=score > 0), 'RESPONSE_QUOTE_REQUIRED')
            if d['dimension_id'] == 'evidence' and current['material'].get('stimulus_text') and score > 0:
                require(RubricEvidenceService._quotes(d.get('source_quotes'), current['material']['stimulus_text']), 'MATERIAL_QUOTE_REQUIRED')
        result[d['dimension_id']] = score
    return result


# Exact local-CPU source selection is deliberately separate from text/LLM scores.
AUDIO_SELECTOR_FIELDS = frozenset(('modality', 'assessment_id', 'media_id', 'candidate_id', 'job_id'))
AUDIO_MISSING_AUTHORITIES = ('original_audio_projection_missing', 'held_execution_attestation_missing')


def audio_selector(value):
    require(type(value) is dict and set(value) == AUDIO_SELECTOR_FIELDS
            and value['modality'] == 'original_audio', 'AUDIO_SELECTOR_REQUIRED')
    require(all(type(value[k]) is str and 0 < len(value[k]) <= 200
                and not any(ord(ch) < 32 for ch in value[k])
                for k in AUDIO_SELECTOR_FIELDS - {'modality'}), 'AUDIO_SELECTOR_REQUIRED')
    return dict(value)


def _audio_numeric(binding, numeric):
    """Validate saved raw numbers with the original adapter, never project scores."""
    if binding['task_family'] == 'read_aloud':
        common = dict(audio_sha256=binding['audio']['sha256'], frames=binding['audio']['frames'],
                      reference_sha256=binding['reference']['text_sha256'],
                      reference_utf8_bytes=binding['reference']['text_utf8_bytes'])
        if binding['audio']['frames'] >= 240000:
            from .pet_multipa_reference_segments import validate_result
            require(set(numeric) == {'schema', 'adapter', 'segments', 'word_count', 'alignment_frame_count'}, 'AUDIO_RAW_INVALID')
            validate_result(dict(common, schema=numeric['schema'], adapter=numeric['adapter'],
                segments=numeric['segments'], reference_word_count=numeric['word_count'],
                alignment_frame_count=numeric['alignment_frame_count']))
        else:
            from .pet_multipa_reference import validate_result
            require(set(numeric) == {'schema', 'adapter', 'sentence', 'words', 'word_count', 'alignment_frames'}, 'AUDIO_RAW_INVALID')
            require(type(numeric['word_count']) is int and numeric['word_count'] == len(numeric['alignment_frames']), 'AUDIO_RAW_INVALID')
            validate_result(dict(common, schema=numeric['schema'], adapter=numeric['adapter'],
                sentence=numeric['sentence'], words=numeric['words'], word_frames=numeric['alignment_frames']))
    else:
        require(binding['task_family'] == 'spoken_response' and set(numeric) == {'sentence', 'words', 'word_count'}, 'AUDIO_RAW_INVALID')
        require(type(numeric['word_count']) is int and 0 < numeric['word_count'] <= 512, 'AUDIO_RAW_INVALID')
        require(type(numeric['sentence']) is dict and set(numeric['sentence']) == {'uttr_acc', 'uttr_fluency', 'uttr_prosodic', 'uttr_total'}
                and type(numeric['words']) is dict and set(numeric['words']) == {'word_acc', 'word_stress', 'word_total'}, 'AUDIO_RAW_INVALID')
        number = lambda v: type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1000000
        require(all(number(v) for v in numeric['sentence'].values())
                and all(type(v) is list and len(v) == numeric['word_count'] and all(number(n) for n in v)
                        for v in numeric['words'].values()), 'AUDIO_RAW_INVALID')


def audio_candidate_source(c, child_id, *, assessment_id, media_id, candidate_id, job_id):
    """Fresh original audio + exact immutable metadata, NOT historical execution proof.

    The connection supplies media access. No caller path/key or feedback model
    call is accepted. No audio/reference/ASR/numeric vector leaves this reader.
    """
    require(c.in_transaction, 'AUDIO_READ_TRANSACTION_REQUIRED')
    from .rubric_calibration_transaction import unsupported_dependency
    unsupported_dependency()  # Never admit physical audio to the text fast cache.
    selector = audio_selector(dict(modality='original_audio', assessment_id=assessment_id,
        media_id=media_id, candidate_id=candidate_id, job_id=job_id))
    from .database import local_media_scope
    import sqlite3
    media_scope = local_media_scope(c)
    require(bool(media_scope), 'AUDIO_LOCAL_MEDIA_SCOPE_REQUIRED')
    try:
        from .pet_multipa_candidate import prepare_input, MODEL
        from .pet_speaking_candidate_store import canonical
        from .pet_speaking_candidate_jobs import digest as job_digest
        from .pet_multipa_epoch import GROUPS, _REVIEWED_DEPLOYMENTS
        from .pet_multipa_protocol import MAX_RESULT
        row = c.execute('SELECT * FROM pet_speaking_model_candidate WHERE child_id=? AND candidate_id=?',
                        (child_id, candidate_id)).fetchone()
        require(row is not None and row['media_id'] == media_id and row['assessment_id'] == assessment_id, 'AUDIO_EXACT_SOURCE_REQUIRED')
        session_id = row['session_id']
        child = c.execute("SELECT 1 FROM child_profile ch JOIN daily_session ds USING(child_id) WHERE ch.child_id=? AND ch.status='active' AND ds.session_id=?",
                          (child_id, session_id)).fetchone()
        require(child is not None, 'AUDIO_EXACT_SOURCE_REQUIRED')
        binding, content = prepare_input(c, session_id, media_id=media_id, assessment_id=assessment_id, **media_scope)
        del content
        require(binding['child_id'] == child_id and canonical(binding) == row['binding_json']
                and row['model'] == MODEL, 'AUDIO_SOURCE_CHANGED')
        ar = c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND assessment_id=?', (child_id, assessment_id)).fetchone()
        require(ar is not None and digest(json.loads(ar['payload_json'])) == ar['fingerprint'] == binding['assessment_fingerprint'], 'AUDIO_ASSESSMENT_CHANGED')
        review = c.execute('SELECT operation FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1', (assessment_id,)).fetchone()
        require(not review or review['operation'] != 'retract', 'AUDIO_ASSESSMENT_RETRACTED')
        latest = c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE candidate_id=? ORDER BY revision DESC LIMIT 1', (candidate_id,)).fetchone()
        require(latest is not None and latest['child_id'] == child_id and latest['revision'] == 1
                and latest['operation'] == 'recorded' and latest['request_hash'] == row['request_hash']
                and is_hash(row['request_hash']), 'AUDIO_CANDIDATE_WITHDRAWN_OR_CHANGED')
        require(type(row['raw_numeric_json']) is str and len(row['raw_numeric_json'].encode()) <= MAX_RESULT, 'AUDIO_RAW_INVALID')
        numeric, assets = json.loads(row['raw_numeric_json']), json.loads(row['assets_json'])
        require(type(numeric) is dict and canonical(numeric) == row['raw_numeric_json']
                and type(assets) is dict and set(assets) == GROUPS and all(is_hash(v) for v in assets.values())
                and canonical(assets) == row['assets_json'], 'AUDIO_RAW_INVALID')
        _audio_numeric(binding, numeric)
        job = c.execute('SELECT * FROM pet_speaking_candidate_job WHERE job_id=? AND child_id=?', (job_id, child_id)).fetchone()
        require(job is not None and job['state'] == 'completed' and job['candidate_id'] == candidate_id
                and all(job[k] == row[k] for k in ('child_id', 'session_id', 'media_id', 'assessment_id', 'binding_json', 'assets_json'))
                and job['binding_sha256'] == job_digest(binding) and job['client_action_id'] == latest['client_action_id']
                and job['owner_token'] is None and job['owner_epoch'] is None and job['error_code'] is None
                and type(job['deployment_key']) is str and bool(job['deployment_key']) and is_hash(job['deployment_sha256']), 'AUDIO_COMPLETED_JOB_REQUIRED')
        require(timestamp(job['created_at']) <= timestamp(row['created_at']) <= timestamp(job['updated_at'])
                <= datetime.now(timezone.utc), 'AUDIO_EXECUTION_METADATA_TIME_INVALID')
        approved = _REVIEWED_DEPLOYMENTS.get(job['deployment_key'])
        blockers = list(AUDIO_MISSING_AUTHORITIES)
        from .pet_multipa_execution_receipt import read_current as read_cpu_execution
        execution=read_cpu_execution(c,child_id,candidate_id=candidate_id,job_id=job_id)
        if execution['held_execution_verified']:blockers.remove('held_execution_attestation_missing')
        elif execution['blocker']!='held_execution_attestation_missing':blockers.append(execution['blocker'])
        if approved is None:
            blockers.append('deployment_metadata_unreviewed')
        elif job_digest(approved) != job['deployment_sha256'] or approved.get('asset_hashes') != assets:
            blockers.append('deployment_configuration_changed')
        media = c.execute('SELECT diagnostic_item_id FROM pet_media_binding WHERE media_id=? AND child_id=? AND session_id=?', (media_id, child_id, session_id)).fetchone()
        require(media is not None, 'AUDIO_EXACT_SOURCE_REQUIRED')
        identity = dict(selector, child_id=child_id, session_id=session_id,
            prompt_id=binding['prompt_id'], diagnostic_item_id=media['diagnostic_item_id'],
            item_version=binding['item_version'], attempt_id=binding['attempt_id'], evidence_id=binding['evidence_id'],
            voice_input_id=binding['voice_input_id'])
        raw_hash, assets_hash = digest(numeric), digest(assets)
        version = digest([identity, binding, raw_hash, assets_hash, row['request_hash'],
                          dict(job), dict(latest), row['created_at']])
        if execution['receipt_sha256']is not None:version=digest([version,execution])
        return {'schema': 'raw_original_audio_candidate_source_v1', 'identity': identity,
            'source_version': version, 'binding_sha256': digest(binding), 'raw_numeric_sha256': raw_hash,
            'asset_hashes_sha256': assets_hash, 'request_hash': row['request_hash'],
            'deployment_sha256': job['deployment_sha256'], 'review_revision': binding['review_revision'],
            'task_family': binding['task_family'], 'audio_sha256': binding['audio']['sha256'],
            'frames': binding['audio']['frames'], 'sample_rate': binding['audio']['sample_rate'],
            'frame_range': [0, binding['audio']['frames']],
            'reference_sha256': binding.get('reference', {}).get('text_sha256'),
            'immutable_metadata_verified': True, 'historical_request_reconstructed': False,
            'held_execution_verified': execution['held_execution_verified'], 'projection_verified': False,
            'blockers': sorted(blockers), 'score_qualified': False, 'mastery_claim_allowed': False,
            'certification_claim_allowed': False, 'local_only': True, 'cloud_transmission_allowed': False}
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        # Never export lower-layer exception messages, plaintext or physical paths.
        require(False, 'AUDIO_SOURCE_NOT_CURRENT')


def audio_projection_source(c,child_id,selector):
    """New v2 view only; preserve every v1 source field and its exact hash."""
    selector=audio_selector(selector)
    source=audio_candidate_source(c,child_id,**{k:selector[k]for k in selector if k!='modality'})
    from .pet_multipa_execution_receipt import read_current
    current=read_current(c,child_id,candidate_id=selector['candidate_id'],job_id=selector['job_id'])
    require(current['held_execution_verified']==source['held_execution_verified'],'AUDIO_EXECUTION_CHANGED')
    return dict(source,execution_receipt_sha256=current['receipt_sha256'])

def audio_label_scores(c,child_id,source,proof):
    """Signed independent calibration label, NOT a parent approval/revision."""
    from .rubric_audio_projection import _score_map
    from .pet_speaking_review import validate_speaking_audio_review
    from .database import local_media_scope
    selector=audio_selector(proof['selector']);identity=source['identity']
    require(identity['child_id']==child_id and all(identity[k]==selector[k]for k in selector),'AUDIO_LABEL_SOURCE_CHANGED')
    require(proof['source_version']==source['source_version']and proof['execution_receipt_sha256']==source['execution_receipt_sha256']and proof['raw_numeric_sha256']==source['raw_numeric_sha256'],'AUDIO_LABEL_SOURCE_CHANGED')
    scope=local_media_scope(c);require(bool(scope),'AUDIO_LOCAL_MEDIA_SCOPE_REQUIRED')
    validate_speaking_audio_review(c,identity['session_id'],media_id=selector['media_id'],assessment_id=selector['assessment_id'],review=proof['review'],**scope)
    return _score_map(proof['scores'])

POLICY = {'version': 'rubric_calibration_policy_v1', 'min_per_anchor_per_stratum': 50,
          'false_pass_upper_max': .05, 'false_reject_upper_max': .10,
          'coverage_min': .95, 'severe_overestimate_max': 0, 'max_valid_days': 30,
          'interval': 'wilson_two_sided_95_upper', 'score_anchors': [0, 1, 2, 3]}


def upper(errors, count):
    if not count:
        return 1.0
    z = 1.959963984540054
    p = errors / count
    return (p + z*z/(2*count) + z*math.sqrt(p*(1-p)/count + z*z/(4*count*count))) / (1 + z*z/count)


def summarize_cases(cases, names, strata):
    """Statistics only, not a certificate issuer. One original group per case."""
    metrics, blockers = {}, []
    for stratum in strata:
        for name in names:
            rows = [r for r in cases if r['stratum'] == stratum]
            anchors = {str(n): sum(r['gold'][name] == n for r in rows) for n in range(4)}
            predictions = [r for r in rows if r['predicted'][name] is not None]
            negatives = [r for r in predictions if r['gold'][name] < 2]
            positives = [r for r in predictions if r['gold'][name] >= 2]
            fp = sum(r['predicted'][name] >= 2 for r in negatives)
            fn = sum(r['predicted'][name] < 2 for r in positives)
            severe = sum(r['predicted'][name] - r['gold'][name] >= 2 for r in predictions)
            value = {'anchors': anchors, 'count': len(rows), 'scored_count': len(predictions),
                'false_pass': fp, 'false_reject': fn, 'false_pass_upper': upper(fp, len(negatives)),
                'false_reject_upper': upper(fn, len(positives)), 'severe_overestimate': severe,
                'coverage': len(predictions)/len(rows) if rows else 0}
            metrics[stratum + ':' + name] = value
            if min(anchors.values()) < POLICY['min_per_anchor_per_stratum']:
                blockers.append('boundary_samples_insufficient')
            if value['false_pass_upper'] > POLICY['false_pass_upper_max'] or value['false_reject_upper'] > POLICY['false_reject_upper_max']:
                blockers.append('error_bound_exceeded')
            if severe > POLICY['severe_overestimate_max']:
                blockers.append('severe_overestimate')
            if value['coverage'] < POLICY['coverage_min']:
                blockers.append('coverage_insufficient')
    return metrics, sorted(set(blockers))

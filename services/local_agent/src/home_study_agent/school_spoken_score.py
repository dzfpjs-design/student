"""Current qualified reasoning transcripts, not acoustic scores or mastery.

Original capture, current question, and current score authority must all survive.
Text-only model calibration is not an oral-answer calibration certificate.
"""
import json
import hashlib

from .local_supply import digest


def _require(value, reason):
    if not value:
        raise ValueError('SCHOOL_SPOKEN_' + reason)


def calibration_only(current):
    return (current.get('calibration_execution') or {}).get('mode') == 'holdout'


def recording_bytes_checked(c, local_path, expected_hash):
    """Check current managed bytes without caching plaintext or a key."""
    from .database import local_media_scope
    from .pet_media_evidence import _read_audio
    scope = local_media_scope(c)
    _require(scope, 'LOCAL_MEDIA_SCOPE_REQUIRED')
    try:
        content = _read_audio(local_path, **scope)
    except FileNotFoundError:
        raise ValueError('SCHOOL_SPOKEN_ORIGINAL_RECORDING_MISSING') from None
    except ValueError as error:
        if str(error) == 'media_decryption_key_required':
            raise PermissionError('SCHOOL_SPOKEN_RECORDING_KEY_UNAVAILABLE') from None
        raise ValueError('SCHOOL_SPOKEN_ORIGINAL_RECORDING_INVALID') from None
    # Other I/O failures remain retryable, not a definitive source withdrawal.
    _require(hashlib.sha256(content).hexdigest() == expected_hash, 'ORIGINAL_RECORDING_CHANGED')
    return len(content)


def _recording(c, prompt, voice, expected_hash):
    from .rubric_calibration_transaction import recording_checked
    row = c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
        (voice,prompt['child_id'],prompt['origin_session_id'])).fetchone()
    _require(row and row[0], 'ORIGINAL_RECORDING_REQUIRED')
    size = recording_bytes_checked(c, row[0], expected_hash)
    recording_checked(c, row[0], expected_hash, size)


def original_response(c, prompt, attempt):
    return _original_response(c,prompt,attempt,require_recording=True)


def original_transcript(c, prompt, attempt):
    """Non-scoring content work may outlive raw audio, not its source records."""
    return _original_response(c,prompt,attempt,require_recording=False)


def _original_response(c, prompt, attempt, *, require_recording):
    if json.loads(prompt['request_json']).get('exam_slot') is not None:
        from .exam_simulation import ExamSimulationService
        from .diagnostic_prompt import DiagnosticPromptService, _version
        from .open_response_profile import profile_for_item
        item = DiagnosticPromptService._item(c,prompt['child_id'],prompt['diagnostic_item_id'])
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
            (item['knowledge_node_id'],)).fetchone() if item else None
        _require(item and node and _version(item)==prompt['item_version']
            and profile_for_item(item,dict(node))['response_mode']=='spoken_explanation','ORIGINAL_MODE_CHANGED')
        origin = ExamSimulationService.original_answer_voice(c,prompt,attempt,require_audio=require_recording)
        if require_recording:
            _recording(c,prompt,origin['source_voice_input_id'],origin['capture_audio_sha256'])
        return dict(version='school_spoken_original_v1',prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
            evidence_id=attempt['evidence_id'],response_hash=digest(attempt['submitted_answer']),origin=origin)
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .open_response_profile import profile_for_item
    from .pet_speaking_origin import original_answer_voice, original_answer_transcript
    item = DiagnosticPromptService._item(c, prompt['child_id'], prompt['diagnostic_item_id'])
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
        (item['knowledge_node_id'],)).fetchone() if item else None
    _require(item and node and _version(item) == prompt['item_version'], 'QUESTION_VERSION_CHANGED')
    profile = profile_for_item(item, dict(node))
    _require(profile['response_mode'] == 'spoken_explanation', 'ORIGINAL_MODE_CHANGED')
    _require(prompt['status'] == 'answered' and prompt['attempt_id'] == attempt['attempt_id']
        and prompt['child_id'] == attempt['child_id']
        and prompt['origin_session_id'] == attempt['session_id']
        and prompt['diagnostic_item_id'] == attempt['diagnostic_item_id'], 'ANSWER_BINDING_CHANGED')
    turn = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
        (prompt['authorized_turn_id'],)).fetchone()
    _require(turn, 'ORIGINAL_VOICE_REQUIRED')
    action = json.loads(turn[0])
    _require(isinstance(action, dict), 'ORIGINAL_VOICE_REQUIRED')
    voice = action.get('source_voice_input_id')
    capture = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?', (voice,)).fetchone()
    _require(capture, 'ORIGINAL_VOICE_REQUIRED')
    reader = original_answer_voice if require_recording else original_answer_transcript
    origin = reader(c, prompt, attempt,
        dict(child_id=prompt['child_id'], session_id=prompt['origin_session_id'],
            prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], voice_input_id=voice), capture[0])
    if require_recording:
        _recording(c, prompt, voice, capture[0])
    return dict(version='school_spoken_original_v1', prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], evidence_id=attempt['evidence_id'],
        response_hash=digest(attempt['submitted_answer']), origin=origin)


def current_score(c, child, assessment_id, prompt, attempt, *, now):
    from .learning_rubric import RubricEvidenceService
    from .school_open_reviews import _time
    # This shared history validator checks source fingerprints and originating
    # steps; its writing-specific original/score checks live in other functions.
    from .writing_score import written_score_candidates, latest_human_review
    service = RubricEvidenceService(None)
    current = service.get(child, assessment_id, connection=c)
    _require(not calibration_only(current), 'CALIBRATION_LABEL_NOT_LEARNING_REVIEW')
    _require(current['source_evidence_id'] == attempt['evidence_id'], 'SCORE_BINDING_CHANGED')
    candidates = written_score_candidates(c, child, attempt['evidence_id'], _current=current)
    selected = latest_human_review(c, child, candidates)
    if not selected:
        qualified = []
        for candidate in candidates:
            try:
                qualified.append(_automatic_score(c, child, candidate, prompt, attempt, now=now))
            except (ValueError, KeyError, TypeError):
                continue
        _require(len(qualified) == 1, 'SCORE_AUTHORITY_AMBIGUOUS')
        _require(qualified[0]['current']['assessment_id'] == assessment_id, 'SCORE_AUTHORITY_NOT_CURRENT')
        return qualified[0]
    _require(selected['assessment_id'] == assessment_id, 'SCORE_AUTHORITY_NOT_CURRENT')
    review = selected['review']
    _require(review['operation'] == 'approve' and current['score_status'] == 'reviewed_human'
        and not review.get('review_profile') and not review.get('audio_evidence')
        and not review.get('writing_evidence'), 'REASONING_REVIEW_REQUIRED')
    _require(current['material'].get('diagnostic_item_id') == prompt['diagnostic_item_id']
        and current['session_id'] == attempt['session_id']
        and current['response'] == attempt['submitted_answer'], 'SCORE_BINDING_CHANGED')
    original = original_response(c, prompt, attempt)
    _require(review.get('school_spoken_original_version') == digest(original), 'REVIEW_ORIGINAL_CHANGED')
    receipt = service.reviewed_receipt(c, child, current['step_id'],
        assessment_id=assessment_id, now=now)
    _require(receipt and receipt['assessment_id'] == assessment_id
        and receipt['diagnostic_item_id'] == prompt['diagnostic_item_id'], 'SCORE_RECEIPT_UNAVAILABLE')
    _require(_time(attempt['created_at']) <= _time(review['created_at']) <= now, 'SCORE_TIME_INVALID')
    return dict(current=current, receipt=receipt,
        decision=dict(status='reviewed_human', authority_id=review['event_id'],
            created_at=review['created_at'], passed=review['passed'], review_revision=current['review_revision']),
        original_binding_version=digest(original), assessment_modality='reviewed_spoken_reasoning_text',
        local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)


def _automatic_score(c, child, current, prompt, attempt, *, now):
    from .learning_rubric import RubricEvidenceService
    from .open_response_profile import profile_for_item
    from .diagnostic_prompt import DiagnosticPromptService
    from .rubric_calibration_evidence import source_snapshot
    from .school_open_reviews import _time
    _require(not current.get('review') and not current.get('review_revision'), 'REASONING_REVIEW_REQUIRED')
    original = original_response(c, prompt, attempt)
    source = source_snapshot(c, child, attempt['evidence_id'])
    _require(current['source_evidence_id'] == attempt['evidence_id']
        and current['material'].get('diagnostic_item_id') == prompt['diagnostic_item_id']
        and current['session_id'] == attempt['session_id']
        and current['response'] == attempt['submitted_answer'], 'SCORE_BINDING_CHANGED')
    receipt = RubricEvidenceService(None).calibrated_receipt(c, child, current['step_id'],
        assessment_id=current['assessment_id'], now=now)
    _require(isinstance(receipt, dict) and receipt.get('score_status') == 'calibrated_model', 'SCORE_CALIBRATION_REQUIRED')
    proof = receipt['calibration_receipt']
    _require(proof.get('score_qualified') is True and proof.get('modality') == 'original_spoken_text'
        and proof['source_version'] == source['version']
        and proof['source_evidence_id'] == attempt['evidence_id']
        and proof['assessment_id'] == current['assessment_id'] == receipt['assessment_id']
        and proof['diagnostic_item_id'] == prompt['diagnostic_item_id'] == receipt['diagnostic_item_id'], 'SCORE_BINDING_CHANGED')
    item = DiagnosticPromptService._item(c, child, prompt['diagnostic_item_id'])
    node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone())
    definitions = profile_for_item(item, node)['dimensions']
    keys = ('dimension_id', 'criterion', 'max_score', 'pass_score', 'score_anchors')
    expected = {d['dimension_id']:{k:d[k] for k in keys} for d in definitions}
    scores = proof['dimensions']
    _require(isinstance(scores, list) and len(scores) == len(expected)
        and {d['dimension_id'] for d in scores} == set(expected), 'SCORE_DIMENSIONS_CHANGED')
    for d in scores:
        _require({k:d[k] for k in keys} == expected[d['dimension_id']]
            and type(d['score']) is int and 0 <= d['score'] <= d['max_score'], 'SCORE_DIMENSIONS_CHANGED')
    passed = all(d['score'] >= d['pass_score'] for d in scores)
    _require(type(proof['passed']) is bool and proof['passed'] == passed
        and type(receipt['correctness']) is int and receipt['correctness'] == int(passed), 'SCORE_RESULT_CHANGED')
    _require(_time(attempt['created_at']) <= _time(current['created_at']) <= now, 'SCORE_TIME_INVALID')
    return dict(current=current, receipt=receipt,
        decision=dict(status='calibrated_model', authority_id=receipt['evidence_id'],
            created_at=current['created_at'], passed=passed, review_revision=0),
        original_binding_version=digest(original), assessment_modality='calibrated_spoken_reasoning_text',
        local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)


def review_origin(c, child, current):
    """Server-owned review binding; never accepted from a review/model payload."""
    _require(not calibration_only(current), 'CALIBRATION_LABEL_NOT_LEARNING_REVIEW')
    attempts = c.execute('SELECT * FROM diagnostic_attempt WHERE child_id=? AND evidence_id=?',
        (child,current['source_evidence_id'])).fetchall()
    _require(len(attempts) == 1, 'ORIGINAL_ATTEMPT_REQUIRED')
    attempt = attempts[0]
    prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
        (child,attempt['attempt_id'])).fetchall()
    _require(len(prompts) == 1 and current['material'].get('diagnostic_item_id') == attempt['diagnostic_item_id']
        and current['session_id'] == attempt['session_id']
        and current['response'] == attempt['submitted_answer'], 'SCORE_BINDING_CHANGED')
    return digest(original_response(c,prompts[0],attempt))


def qualified_sources(c, child, rows, *, now):
    """Live oral observations for reports/plans, without writing-score fallback."""
    from .writing_score import written_score_candidates
    from .school_open_reviews import _time
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'rubric_assessment','diagnostic_prompt','diagnostic_attempt','voice_capture_binding'} <= tables:
        return
    for row in rows:
        try:
            attempts = c.execute('''SELECT a.* FROM diagnostic_attempt a
                JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
                  AND e.session_id=a.session_id AND e.evidence_type='diagnostic_answer'
                JOIN diagnostic_item d ON d.diagnostic_item_id=a.diagnostic_item_id
                  AND d.knowledge_node_id=e.knowledge_node_id
                JOIN knowledge_node n ON n.knowledge_node_id=d.knowledge_node_id
                WHERE a.child_id=? AND a.evidence_id=? AND d.answer_type='open_response'
                  AND n.track='school' AND a.created_at=e.occurred_at''', (child,row['evidence_id'])).fetchall()
            if len(attempts) != 1 or _time(attempts[0]['created_at']) > now:
                continue
            attempt = attempts[0]
            prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE attempt_id=? AND child_id=?',
                (attempt['attempt_id'],child)).fetchall()
            if len(prompts) != 1:
                continue
            original_response(c,prompts[0],attempt)
            candidates = written_score_candidates(c,child,row['evidence_id'])
            accepted = []
            for candidate in candidates:
                try:
                    accepted.append(current_score(c,child,candidate['assessment_id'],prompts[0],attempt,now=now))
                except (ValueError,KeyError,TypeError):
                    continue
            if len(accepted) == 1:
                yield row, accepted[0]
        except (ValueError,KeyError,TypeError):
            continue

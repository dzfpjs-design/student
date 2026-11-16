"""Versioned human audio rubric, not a calibrated automatic PET examination."""
PROFILE = 'pet_speaking_audio_v1'
CRITERIA = {
    'audio_text_alignment': '核对原音与原回答一致；识别转写遗漏或错误，不能把转写文本当作已听到的内容。',
    'intelligibility': '原音的主要意思可理解；根据具体语音片段判断，不用音量或字数替代。',
    'pronunciation': '结合原音判断发音是否影响表达与理解；口音差异本身不是错误。',
    'prosody': '重音、停顿和语调支持本次表达；不得从转写标点推断。',
    'fluency': '根据完整原音判断表达是否连贯，具体指出影响表达的中断；不以语速或静音数量代替。',
}


def dimensions():
    from .learning_rubric import rubric_dimensions, SCORE_ANCHORS
    return [dict(d, score=None, response_quotes=[], rationale='not_assessed') for d in rubric_dimensions('英语')] + [dict(dimension_id=name, criterion=text, max_score=3, pass_score=2,
        score_anchors=dict(SCORE_ANCHORS), score=None, response_quotes=[], rationale='not_assessed')
        for name, text in CRITERIA.items()]


def is_speaking(c, item_id):
    row = c.execute('''SELECT kn.track,kn.code,di.answer_type FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?''', (item_id,)).fetchone()
    return bool(row and row['track'] == 'pet' and (row['code'].startswith('PET.SPEAKING.') or row['answer_type'] in {'speaking', 'read_aloud'}))


def current_audio_review(c, child_id, current, *, media_scope=None):
    """Verify persisted review provenance; callers still apply their learning gate."""
    from .database import local_media_scope
    from .learning_rubric import RubricEvidenceService
    from .pet_speaking_review import validate_speaking_audio_review
    if not child_id or current.get('child_id') != child_id:
        return False
    review = current.get('review')
    if not review or review.get('operation') != 'approve' or review.get('review_profile') != PROFILE:
        return False
    owner = c.execute('SELECT child_id FROM rubric_assessment WHERE assessment_id=?',
                      (current.get('assessment_id'),)).fetchone()
    latest = c.execute('SELECT * FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1',
                       (current.get('assessment_id'),)).fetchone()
    # A saved projection is not current authority after a retraction or replacement.
    if not owner or owner['child_id'] != child_id or not latest or latest['child_id'] != child_id:
        return False
    try:
        if review != RubricEvidenceService._event(latest) or current.get('review_revision') != latest['revision']:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    scope = media_scope or local_media_scope(c)
    if not scope or current['source_status'] != 'current':
        return False
    evidence = review.get('audio_evidence')
    if not isinstance(evidence, dict) or set(evidence) != {'media_id', 'review'}:
        return False
    try:
        validate_speaking_audio_review(c, current['session_id'], media_id=evidence['media_id'],
            assessment_id=current['assessment_id'], review=evidence['review'], **scope)
        definitions = {d['dimension_id']: d for d in dimensions()}
        scored = review['dimensions']
        return (len(scored) == len(definitions) and {d['dimension_id'] for d in scored} == set(definitions)
                and all(type(d['score']) is int and 0 <= d['score'] <= definitions[d['dimension_id']]['max_score'] for d in scored)
                and review['passed'] == all(d['score'] >= definitions[d['dimension_id']]['pass_score'] for d in scored))
    except (ValueError, TypeError, KeyError):
        return False


def scoped_speaking_gate(c, child_id, item_id, *, session_id=None, prompt_id=None, item_version=None,
                         attempt_id=None, evidence_id=None, as_of=None, answer=None, media_root=None, data_encryption_key=None):
    from .learning_rubric import RubricEvidenceService, _time
    from datetime import datetime, timezone
    unknown = {'status': 'unknown', 'skill_scoring_allowed': False,
               'blockers': ['speaking_original_audio_review_required']}
    if not all((session_id, prompt_id, item_version, attempt_id, evidence_id)):
        return unknown
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'diagnostic_prompt', 'rubric_assessment', 'rubric_review_event'} <= tables:
        return unknown
    original = c.execute('''SELECT da.submitted_answer FROM diagnostic_prompt dp JOIN diagnostic_attempt da
        ON da.attempt_id=dp.attempt_id AND da.child_id=dp.child_id AND da.session_id=dp.origin_session_id
        WHERE dp.prompt_id=? AND dp.child_id=? AND dp.origin_session_id=? AND dp.item_version=?
        AND dp.status='answered' AND da.attempt_id=? AND da.evidence_id=? AND da.diagnostic_item_id=?''',
        (prompt_id, child_id, session_id, item_version, attempt_id, evidence_id, item_id)).fetchone()
    if original is None or answer is not None and original['submitted_answer'] != answer:
        return unknown
    now = _time(as_of) if as_of else datetime.now(timezone.utc)
    service = RubricEvidenceService(None)
    currents = [service.get(child_id, row[0], connection=c) for row in c.execute(
        "SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND json_extract(payload_json,'$.source_evidence_id')=?", (child_id, evidence_id))]
    reviewed = [current for current in currents if current['review']]
    if not reviewed:
        return unknown
    # A later failure/retraction/text-only review must not fall back to an older
    # successful audio review of the same answer, even on a different agent step.
    current = max(reviewed, key=lambda v: (_time(v['review']['created_at']), v['review']['event_id']))
    scope = {'media_root': media_root, 'data_encryption_key': data_encryption_key} if media_root is not None else None
    if _time(current['review']['created_at']) <= now and current_audio_review(c, child_id, current, media_scope=scope):
        passed = current['review']['passed'] and current['review']['independent'] and current['review']['hint_level'] == 0
        return {'status': 'reviewed_human', 'skill_scoring_allowed': bool(passed),
                'blockers': [] if passed else ['speaking_audio_review_not_independent_correct'],
                'assessment_scope': 'reviewed_human_original_audio', 'assessment_id': current['assessment_id'],
                'review_revision': current['review_revision']}
    return unknown

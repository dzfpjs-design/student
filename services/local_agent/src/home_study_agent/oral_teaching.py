"""Local read-only oral feedback from existing original-audio review receipts.

No schema initialization, transaction ownership, media playback, scoring, model
dispatch or prompt creation occurs here. The host must recheck feedback_version
inside its admitted action transaction before creating a new capture/prompt.
"""
from datetime import datetime, timezone
import json

from .diagnostic_prompt import DiagnosticPromptService, _version
from .learning_obligations import withdrawn_evidence_ids
from .learning_rubric import RubricEvidenceService
from .local_supply import digest
from .pet_audio_rubric import dimensions
from .specialized_stages import _item_source
from .teaching_context import EVIDENCE_TTL, _time
from .teaching_modality_evidence import project_reviewed_teaching_evidence


VERSION = 'oral_teaching_v2'


class OralTeachingScopeError(ValueError):
    """The caller's frozen child/session/prompt/version does not match."""


def project_oral_teaching(c, child_id, session_id, prompt_id, item_version, *, as_of=None):
    if not c.in_transaction:
        raise ValueError('ORAL_TEACHING_READ_TRANSACTION_REQUIRED')
    now = (as_of if isinstance(as_of, datetime) else _time(as_of)) if as_of is not None else datetime.now(timezone.utc)
    if now is None or now.tzinfo is None:
        raise ValueError('ORAL_TEACHING_AS_OF_INVALID')
    row = c.execute('''SELECT p.*, cp.status AS child_status FROM diagnostic_prompt p
        JOIN daily_session ds ON ds.session_id=p.origin_session_id AND ds.child_id=p.child_id
        JOIN child_profile cp ON cp.child_id=p.child_id
        WHERE p.prompt_id=? AND p.child_id=? AND p.origin_session_id=? AND p.item_version=?''',
        (prompt_id, child_id, session_id, item_version)).fetchone()
    if row is None:
        raise OralTeachingScopeError('ORAL_TEACHING_SCOPE_CHANGED')
    result = dict(version=VERSION, child_id=child_id, session_id=session_id,
        prompt_id=prompt_id, item_version=item_version, diagnostic_item_id=row['diagnostic_item_id'],
        status='unavailable', reason=None, source_version=None, original=None,
        question=None, task_points=[], response_text=None, response_authority=None,
        review_ref=None, audio_work_ref=None, feedback=[], spoken_feedback='原题或原回答需要重新核对，没有沿用旧评分。',
        next_action=None, independent_observed=False, skill_scoring_allowed=False,
        mastery_claim_allowed=False, local_only=True, cloud_transmission_allowed=False)
    authority = {'prompt_status': row['status'], 'child_status': row['child_status']}

    def finish(reason=None):
        result['reason'] = reason
        # Exclude clock ticks: a version changes only with relevant facts or gates.
        result['feedback_version'] = digest([result, authority])
        if result['next_action']:
            result['next_action']['binding'] = dict(child_id=child_id, session_id=session_id,
                prompt_id=prompt_id, item_version=item_version, source_version=result['source_version'],
                original=result['original'], review_ref=result['review_ref'],
                feedback_version=result['feedback_version'])
        return result

    if row['child_status'] != 'active' or row['status'] in {'cancelled', 'invalidated'}:
        return finish('original_scope_inactive')
    item = DiagnosticPromptService._item(c, child_id, row['diagnostic_item_id'])
    if not item or _version(item) != item_version:
        return finish('original_source_changed')
    expected = json.loads(item['expected_answer_json'])
    school_spoken = (item['answer_type'] == 'open_response'
        and item['assessment_mode'] == 'open_response_rubric'
        and expected.get('response_mode') == 'spoken_explanation')
    if item['answer_type'] not in {'speaking', 'read_aloud'} and not school_spoken:
        return finish('oral_response_required')
    source, reason = _item_source(c, child_id, item, now)
    if not source or not source['score_qualified']:
        return finish(reason or 'oral_source_not_qualified')
    points = expected.get('task_points')
    if not isinstance(points, list) or not points or any(not isinstance(p, str) or not p.strip() for p in points):
        return finish('oral_task_points_unavailable')
    result.update(source_version=source['source_version'], question=item['prompt'],
        task_points=[dict(task_point=p, status='not_assessed') for p in points])
    if row['status'] != 'answered' or not row['attempt_id']:
        result.update(status='waiting_answer', spoken_feedback='请先完成这道原题，尚未形成原回答反馈。')
        return finish('original_answer_required')
    attempt = c.execute('''SELECT a.*, e.occurred_at FROM diagnostic_attempt a
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
          AND e.session_id=a.session_id AND e.knowledge_node_id=?
        WHERE a.attempt_id=? AND a.child_id=? AND a.session_id=? AND a.diagnostic_item_id=?''',
        (item['knowledge_node_id'], row['attempt_id'], child_id, session_id, item['diagnostic_item_id'])).fetchone()
    if not attempt or attempt['evidence_id'] in withdrawn_evidence_ids(c, child_id):
        return finish('original_evidence_unavailable')
    at = _time(attempt['occurred_at'])
    if not at or at > now or at != _time(attempt['created_at']):
        return finish('original_evidence_time_invalid')
    result['original'] = dict(attempt_id=attempt['attempt_id'], evidence_id=attempt['evidence_id'],
        response_hash=digest(attempt['submitted_answer']))
    if now - at > EVIDENCE_TTL:
        return finish('evidence_expired')
    if school_spoken:
        from .school_spoken_score import original_transcript
        try:
            authority['school_original'] = original_transcript(c, row, attempt)
        except ValueError as error:
            return finish(str(error))
    result.update(status='content_self_check', response_text=attempt['submitted_answer'],
        response_authority='unverified_transcript_not_heard_audio',
        spoken_feedback='我们核对一下：' + points[0] + ('。你想再读一次吗？'
            if item['answer_type'] == 'read_aloud' else '。你想补充一句吗？'),
        next_action=dict(kind='self_check_original_task', task_point=points[0],
            requires_runtime_admission=True, requires_explicit_voice_request=True, requires_new_prompt_and_capture=True,
            independent_check=False))
    if school_spoken:
        # School reasoning uses its own rubric/calibration path, never PET
        # acoustic dimensions. A bound transcript supports practice, not a score.
        result['response_authority'] = 'original_capture_bound_transcript_not_acoustic_review'
        return finish()
    projection = project_reviewed_teaching_evidence(c, child_id, item['knowledge_node_id'], now)
    # Keep only this answer's current provenance; unrelated attempts cannot steal
    # focus. The reused projection itself still checks full-child exposure/aliases.
    authority['excluded'] = [e for e in projection['excluded_evidence']
                             if e['evidence_id'] == attempt['evidence_id']]
    assessment_rows = c.execute('''SELECT r.assessment_id FROM rubric_assessment r
        JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
        WHERE r.child_id=? AND p.child_id=r.child_id AND p.session_id=?
        AND json_extract(r.payload_json,'$.source_evidence_id')=? ORDER BY r.assessment_id''',
        (child_id, session_id, attempt['evidence_id'])).fetchall()
    service = RubricEvidenceService(None)
    current_by_id = {r[0]: service.get(child_id, r[0], connection=c) for r in assessment_rows}
    authority['reviews'] = [dict(assessment_id=a['assessment_id'], source_status=a['source_status'],
        review_revision=a['review_revision'], review_digest=digest(a['review'])) for a in current_by_id.values()]
    receipts = [r for r in projection['effective_evidence'] if r['evidence_id'] == attempt['evidence_id']]
    if len(receipts) != 1:
        from .pet_audio_projection import apply_formative_feedback
        result = apply_formative_feedback(c, result)
        if result['status'] == 'formative_audio_feedback':
            return finish()
        return finish('original_audio_review_required')
    receipt = receipts[0]
    ref = receipt['review_refs'][0]
    current = current_by_id.get(ref['assessment_id'])
    if not current or current['review_revision'] != ref['review_revision']:
        return finish('original_audio_review_changed')
    review = current['review']
    definitions = {d['dimension_id']: d for d in dimensions()}
    observations = {o['dimension_id']: o for o in review['audio_evidence']['review']['observations']}
    for d in review['dimensions']:
        definition = definitions[d['dimension_id']]
        result['feedback'].append(dict(dimension_id=d['dimension_id'], criterion=definition['criterion'],
            score=d['score'], pass_score=definition['pass_score'], response_quotes=d['response_quotes'],
            rationale=d['rationale'], audio_observation=observations.get(d['dimension_id'])))
    # No entry is interpreted as a missing task point. Only an exact task-point
    # reviewer citation can be displayed as support, not as a coverage/skill score.
    for p in result['task_points']:
        quotes = [entry['response_quote'] for entry in review.get('criterion_evidence', [])
                  if entry['criterion'] == p['task_point']]
        if quotes:
            p.update(status='reviewer_cited', response_quotes=quotes)
    result.update(status='reviewed_audio_feedback', review_ref=ref,
        response_authority='transcript_with_versioned_human_original_audio_review',
        independent_observed=bool(receipt['independent'] and receipt['hint_level'] == 0),
        skill_scoring_allowed=bool(receipt['correctness'] and receipt['independent'] and receipt['hint_level'] == 0))
    chosen = next((f for f in result['feedback'] if f['score'] < f['pass_score']), result['feedback'][0])
    result['spoken_feedback'] = chosen['rationale']
    result['next_action'] = dict(kind='retry_original_supported', dimension_id=chosen['dimension_id'],
        task_point=points[0], requires_runtime_admission=True, requires_explicit_voice_request=True,
        requires_new_prompt_and_capture=True, independent_check=False)
    authority['receipt'] = receipt
    return finish()

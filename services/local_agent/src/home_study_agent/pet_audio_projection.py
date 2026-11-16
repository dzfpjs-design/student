"""Read-only formative advice. Never an assessment, score or mastery receipt."""
import json
from datetime import datetime

from .database import local_media_scope
from .local_supply import digest
from .pet_audio_gateway import decode_feedback
from .pet_audio_source import consent, now, source


def current_feedback(c, child_id, session_id, prompt_id=None):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_audio_work'").fetchone():
        return []
    scope = local_media_scope(c)
    if not scope:
        return []
    rows = c.execute("SELECT * FROM pet_audio_work WHERE child_id=? AND session_id=? AND state IN ('response_saved','delivered') ORDER BY created_at,work_id",
        (child_id, session_id)).fetchall()
    result = []
    for row in rows:
        try:
            binding = json.loads(row['binding_json'])
            if prompt_id is not None and binding['prompt_id'] != prompt_id:
                continue
            grant = c.execute('SELECT * FROM pet_audio_grant WHERE child_id=? ORDER BY revision DESC LIMIT 1', (child_id,)).fetchone()
            epoch = c.execute('SELECT epoch FROM pet_audio_epoch WHERE singleton=1').fetchone()
            config = c.execute('SELECT endpoint_hash FROM pet_audio_config WHERE singleton=1').fetchone()
            if (not grant or not grant['allowed'] or grant['grant_id'] != row['grant_id']
                    or grant['epoch'] != row['epoch'] or not epoch or epoch[0] != row['epoch']
                    or not config or config[0] != row['endpoint_hash'] or config[0] != grant['endpoint_hash']
                    or datetime.fromisoformat(grant['expires_at']) <= now() or grant['consent_hash'] != consent(c)):
                continue
            original = source(c, child_id, row['assessment_id'], **scope)
            if original['source_hash'] != row['source_hash']:
                continue
            value = json.loads(row['result_json'])
            feedback = decode_feedback(json.dumps(value['feedback']), duration_ms=binding['duration_ms'])
            ref = {'work_id': row['work_id'], 'generation': row['generation'],
                   'source_hash': row['source_hash'], 'result_hash': digest(value)}
            result.append({'ref': ref, 'prompt_id': binding['prompt_id'], 'item_version': binding['item_version'],
                'question': original['question'], 'feedback': feedback,
                'requested_model': value['requested_model'], 'reported_model': value['reported_model'],
                'mastery_claim_allowed': False, 'assessment_scope': 'uncalibrated_original_audio_practice',
                'delivery_scope': 'runtime_response_saved_not_audible', 'state': row['state']})
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return result


def apply_formative_feedback(c, result):
    values = current_feedback(c, result['child_id'], result['session_id'], result['prompt_id'])
    if len(values) != 1:
        return result
    value = values[0]
    observed = [row for row in value['feedback']['observations'] if row['status'] == 'observed']
    chosen = observed[0] if observed else None
    spoken = ('这是原音练习建议，不是可靠评分。' + chosen['observation'] + '。' + chosen['exercise']
              if chosen else '原音已经保存，但这次还不能给出确定的发音建议。我们按原题再练一次，好吗？')
    return {**result, 'status': 'formative_audio_feedback', 'audio_work_ref': value['ref'],
        'response_authority': 'uncalibrated_model_original_audio_advice',
        'feedback': value['feedback']['observations'], 'spoken_feedback': spoken,
        'independent_observed': False, 'skill_scoring_allowed': False, 'mastery_claim_allowed': False}

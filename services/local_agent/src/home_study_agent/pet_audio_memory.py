"""Advice and practice history only. Separate from exact learning observations."""
import json

from .database import local_media_scope
from .local_supply import digest
from .pet_audio_projection import current_feedback
from .pet_audio_source import source

EMPTY_FINGERPRINT = digest([])


def report_memory(c, child_id, session_id):
    owned = not c.in_transaction
    if owned:
        c.execute('BEGIN')
    try:
        facts = current_feedback(c, child_id, session_id)
        result = []
        scope = local_media_scope(c)
        for fact in facts:
            practices = []
            for row in c.execute('''SELECT p.prompt_id,p.request_json,r.assessment_id FROM diagnostic_prompt p
                JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id AND a.child_id=p.child_id AND a.session_id=p.origin_session_id
                JOIN rubric_assessment r ON r.child_id=a.child_id AND json_extract(r.payload_json,'$.source_evidence_id')=a.evidence_id
                WHERE p.child_id=? AND p.origin_session_id=? AND p.status='answered' ''', (child_id, session_id)):
                try:
                    follow = json.loads(row['request_json']).get('oral_followup', {}).get('action', {})
                    if follow.get('audio_work_ref') != fact['ref'] or follow.get('parent_prompt_id') != fact['prompt_id']:
                        continue
                    original = source(c, child_id, row['assessment_id'], **scope)
                    if original['binding']['prompt_id'] == fact['prompt_id']:
                        continue
                    practices.append({'prompt_id': row['prompt_id'], 'attempt_id': original['binding']['attempt_id'],
                        'source_hash': original['source_hash'], 'independent_check': bool(follow.get('independent_check')),
                        'mastery_claim_allowed': False})
                except (ValueError, TypeError, KeyError, OSError):
                    continue
            # Delivery status does not turn an observation into a learning fact.
            result.append({'ref': fact['ref'], 'question': fact['question'], 'feedback': fact['feedback'],
                'practices': practices, 'assessment_scope': fact['assessment_scope'],
                'mastery_claim_allowed': False, 'skill_scoring_allowed': False,
                'description': '原音练习建议，未经可靠评分校准；复练记录不表示已掌握。'})
        return {'items': result, 'fingerprint': digest(result)}
    finally:
        if owned:
            c.rollback()

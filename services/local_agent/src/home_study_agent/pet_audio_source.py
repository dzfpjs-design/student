"""Exact original-answer projection for a separately authorized audio purpose."""
import json
from datetime import datetime, timezone

from .learning_rubric import RubricEvidenceService
from .local_supply import digest
from .pet_speaking_review import _prepare
from .pet_audio_gateway import MAX_DURATION_MS, MAX_WAV_BYTES


class PetAudioAuthorityError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise PetAudioAuthorityError('PET_AUDIO_' + code)


def source(c, child_id, assessment_id, *, media_root, data_encryption_key=None, live=False):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    current = RubricEvidenceService(None).get(child_id, assessment_id, connection=c)
    require(current['source_status'] == 'current' and not current['review_revision'], 'SOURCE_REVIEW_CHANGED')
    prompt = c.execute('''SELECT q.*,a.submitted_answer FROM diagnostic_prompt q
        JOIN diagnostic_attempt a ON a.attempt_id=q.attempt_id AND a.child_id=q.child_id
          AND a.session_id=q.origin_session_id AND a.diagnostic_item_id=q.diagnostic_item_id
        WHERE q.child_id=? AND q.origin_session_id=? AND a.evidence_id=?''',
        (child_id, current['session_id'], current['source_evidence_id'])).fetchall()
    require(len(prompt) == 1, 'ORIGINAL_PROMPT_REQUIRED')
    prompt = prompt[0]
    voice_turn = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (prompt['authorized_turn_id'], child_id, current['session_id'])).fetchone()
    require(voice_turn is not None, 'ORIGINAL_VOICE_REQUIRED')
    original_voice = json.loads(voice_turn[0]).get('source_voice_input_id')
    bindings = c.execute('''SELECT media_id FROM pet_media_binding WHERE child_id=? AND session_id=?
        AND prompt_id=? AND item_version=? AND kind='speaking' AND voice_input_id=?''',
        (child_id, current['session_id'], prompt['prompt_id'], prompt['item_version'], original_voice)).fetchall()
    require(len(bindings) == 1, 'EXACT_AUDIO_REQUIRED')
    packet, audio = _prepare(c, current['session_id'], media_id=bindings[0][0], assessment_id=assessment_id,
        media_root=media_root, data_encryption_key=data_encryption_key, propagate_io=True)
    require(packet['child_id'] == child_id and packet['attempt_id'] == prompt['attempt_id'], 'OWNER_CHANGED')
    duration_ms = (packet['audio']['frames'] * 1000 + packet['audio']['sample_rate'] - 1) // packet['audio']['sample_rate']
    require(0 < duration_ms <= MAX_DURATION_MS and len(audio) <= MAX_WAV_BYTES, 'AUDIO_LIMIT')
    runtime = c.execute('''SELECT o.objective_id,o.status AS objective_status,ds.status AS session_status,
        ds.state,ds.observation_status,cp.status AS child_status,t.tool_status,s.status AS step_status,p.status AS plan_status,
        s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
        JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
        JOIN agent_objective o ON o.objective_id=p.objective_id AND o.child_id=p.child_id AND o.session_id=p.session_id
        JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=p.child_id
        JOIN child_profile cp ON cp.child_id=p.child_id WHERE s.step_id=? AND p.child_id=? AND p.session_id=?''',
        (current['step_id'], child_id, current['session_id'])).fetchone()
    require(runtime is not None and runtime['child_status'] == 'active'
        and runtime['tool_status'] != 'cancelled' and runtime['step_status'] == 'completed'
        and runtime['plan_status'] == 'completed', 'RUNTIME_SOURCE_CHANGED')
    args = json.loads(runtime['arguments_json'])
    require(args.get('evidence_id') == current['source_evidence_id']
        and args.get('response') == current['response'] == prompt['submitted_answer']
        and args.get('question_asked') == current['question'], 'RUNTIME_ANSWER_CHANGED')
    if live:
        focus = c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=? ORDER BY rowid DESC LIMIT 1',
            (child_id, current['session_id'])).fetchone()
        other = c.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND status='active' AND objective_id!=?",
            (child_id, runtime['objective_id'])).fetchone()
        require(runtime['objective_status'] == 'active' and runtime['session_status'] == 'active'
            and runtime['state'] not in {'evolving', 'completed'} and runtime['observation_status'] != 'paused'
            and focus and focus[0] == prompt['prompt_id'] and not other, 'LIVE_SCOPE_CHANGED')
    binding = {key: packet[key] for key in ('child_id', 'session_id', 'assessment_id', 'prompt_id', 'item_version',
        'diagnostic_item_id', 'media_id', 'attempt_id', 'evidence_id', 'audio_sha256', 'answer_voice_origin', 'binding_version')}
    binding.update(objective_id=runtime['objective_id'], step_id=current['step_id'], duration_ms=duration_ms,
        question_hash=digest(current['question']), review_revision=current['review_revision'])
    transitions = [tuple(r) for r in c.execute('SELECT * FROM session_transition WHERE session_id=? ORDER BY rowid', (current['session_id'],))]
    return {'binding': binding, 'source_hash': digest(binding), 'question': current['question'], 'audio': audio,
            'live_hash': digest([dict(runtime), transitions])}


def consent(c):
    privacy = c.execute('SELECT cloud_audio_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    latest = c.execute("SELECT * FROM consent_record WHERE scope='cloud_audio_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
    require(privacy and privacy[0] == 1 and (not latest or latest['allowed'] == 1), 'AUDIO_CONSENT_REQUIRED')
    return digest([tuple(privacy), dict(latest) if latest else None])


def now():
    return datetime.now(timezone.utc)

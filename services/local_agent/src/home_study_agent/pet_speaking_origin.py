"""Read-only capture provenance of an original spoken answer.

Historical tasks need not remain active. Text teaching revalidates the submitted
prompt, authorized turn and captured transcript without requiring retained audio.
Audio-dependent callers must use original_answer_voice and verify actual bytes;
neither reader by itself grants score or mastery authority.
"""
import json
from datetime import datetime

from .agent_runtime import AgentRuntimeStore


def _require(condition):
    if not condition:
        raise ValueError('PET_SPEAKING_REVIEW_ORIGINAL_VOICE_BINDING_REQUIRED')


def original_answer_voice(c, prompt, attempt, media, audio_sha256):
    return _original_answer(c,prompt,attempt,media,audio_sha256,require_audio=True)


def original_answer_transcript(c, prompt, attempt, media, audio_sha256):
    """Capture identity only, not retained bytes, heard speech or a score.

    Return the same original identity so raw-file retention cannot rewrite an
    already accepted answer. Callers must never use this to approve audio scores.
    """
    return _original_answer(c,prompt,attempt,media,audio_sha256,require_audio=False)


def _original_answer(c, prompt, attempt, media, audio_sha256, *, require_audio):
    from .document_spoken_supplement import original
    supplement = original(c,prompt)
    if supplement is not None:
        turn=c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?',(prompt['authorized_turn_id'],)).fetchone()
        _require(turn and turn[0]==attempt['submitted_answer']
            and media['child_id']==prompt['child_id'] and media['session_id']==prompt['origin_session_id']
            and media['prompt_id']==prompt['prompt_id'] and media['item_version']==prompt['item_version']
            and supplement['source_voice_input_id']==media['voice_input_id']
            and supplement['capture_audio_sha256']==audio_sha256)
        if require_audio:
            voice = c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
                (media['voice_input_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
            _require(voice and voice[0])
        return supplement
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _require({'agent_turn', 'agent_plan', 'agent_step', 'voice_capture_binding'} <= tables)
    _require('authorized_turn_id' in prompt.keys() and prompt['authorized_turn_id'])
    owner = (media['child_id'], media['session_id'])
    current = c.execute("""SELECT t.*,p.status AS plan_status,s.capability_name,s.arguments_json
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
          AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?""",
        (prompt['authorized_turn_id'], *owner)).fetchone()
    _require(current and current['capability_name'] == 'diagnostic.submit'
             and current['intent'] != 'runtime_continuation'
             and current['tool_status'] != 'cancelled' and current['plan_status'] != 'cancelled')
    action = json.loads(current['action_json'])
    _require(isinstance(action, dict))
    original_voice = action.get('source_voice_input_id')
    _require(isinstance(original_voice, str) and original_voice
             and original_voice == media['voice_input_id'])
    binding = {key: media[key] for key in ('prompt_id', 'item_version')}
    _require(binding == {'prompt_id': action.get('diagnostic_prompt_id'),
                         'item_version': action.get('diagnostic_item_version')})
    source_id = action.get('answer_source_turn_id')
    source = current
    if source_id:
        _require(AgentRuntimeStore.is_answer_role_confirmation(current['transcript']))
        source = c.execute("""SELECT t.*,p.status AS plan_status FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
              AND p.child_id=t.child_id AND p.session_id=t.session_id
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?""", (source_id, *owner)).fetchone()
        previous = c.execute("""SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=?
            AND rowid<(SELECT rowid FROM agent_turn WHERE agent_turn_id=?) ORDER BY rowid DESC LIMIT 1""",
            (*owner, current['agent_turn_id'])).fetchone()
        _require(source and previous and previous[0] == source_id
                 and source['tool_status'] != 'cancelled' and source['plan_status'] != 'cancelled')
        saved = json.loads(source['action_json'])
        _require(isinstance(saved, dict))
        candidate = saved.get('pending_answer_candidate') or {}
        _require(isinstance(candidate, dict))
        target = candidate.get('target') or {}
        _require(isinstance(target, dict))
        arguments = json.loads(current['arguments_json'])
        _require(isinstance(arguments, dict))
        _require(saved.get('source') == 'pending_answer_clarification' and saved.get('local_only') is True
                 and action.get('local_only') is True and target.get('action') == 'submit_diagnostic_answer'
                 and target.get('binding') == binding and candidate.get('answer') == source['transcript']
                 and arguments.get('answer') == source['transcript']
                 and attempt['submitted_answer'] == source['transcript'])
    else:
        _require(not AgentRuntimeStore.local_answer_requires_clarification(source['transcript'])
                 and attempt['submitted_answer'] in AgentRuntimeStore.local_answer_forms(source['transcript'], 'diagnostic.submit'))
    times = [datetime.fromisoformat(row['created_at']) for row in (prompt, source, current, attempt)]
    _require(all(value.tzinfo is not None for value in times) and all(a <= b for a, b in zip(times, times[1:])))
    # This existing helper validates capture snapshots and original transcripts,
    # without imposing live task/current-turn restrictions on historical facts.
    metadata = AgentRuntimeStore._answer_voice_metadata(c, current, source, 'diagnostic.submit', binding)
    _require(metadata.get('source_voice_input_id') == original_voice
        and (not require_audio or metadata.get('raw_audio_available')))
    capture = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?', (original_voice,)).fetchone()
    _require(capture and capture[0] == audio_sha256)
    return {'version': 'pet_original_answer_voice_v1', 'authorized_turn_id': current['agent_turn_id'],
            'source_turn_id': source['agent_turn_id'], 'source_voice_input_id': original_voice,
            'voice_input_id': metadata.get('voice_input_id'), 'capture_audio_sha256': capture[0]}

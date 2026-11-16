"""The actual answer voice, not a same-prompt confirmation, owns oral review."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from test_pet_media_evidence import media, CHILD
from test_pet_audio_rubric_review import prepared


@pytest.mark.parametrize('fault', ['no_authorization', 'missing_source', 'null_source', 'other_source',
    'candidate_changed', 'source_cancelled', 'capture_cancelled', 'capture_version', 'capture_sha', 'capture_stale'])
def test_saved_audio_review_revalidates_the_final_original_answer_voice(media, fault):
    db, service, assessment, body, prompt = prepared(media)
    service.review(CHILD, assessment['assessment_id'], **body)
    assert service.qualification(CHILD, assessment['assessment_id'])['eligible_for_mastery_gate']
    with db.connect() as c:
        original = c.execute('SELECT authorized_turn_id FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0]
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (original,)).fetchone()
        action = json.loads(turn['action_json'])
        source_id = action.get('answer_source_turn_id') or original
        if fault == 'no_authorization':
            c.execute('UPDATE diagnostic_prompt SET authorized_turn_id=NULL WHERE prompt_id=?', (prompt['prompt_id'],))
        elif fault in {'missing_source', 'null_source', 'other_source'}:
            if fault == 'missing_source': action.pop('source_voice_input_id')
            else: action['source_voice_input_id'] = None if fault == 'null_source' else 'some-confirmation-voice'
            c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action), original))
        elif fault == 'candidate_changed':
            c.execute("UPDATE agent_turn SET transcript='changed original answer' WHERE agent_turn_id=?", (source_id,))
        elif fault == 'source_cancelled':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (source_id,))
        elif fault == 'capture_cancelled':
            c.execute("UPDATE voice_capture_binding SET status='cancelled'")
        elif fault == 'capture_version':
            c.execute("UPDATE voice_capture_binding SET item_version='changed'")
        elif fault == 'capture_stale':
            c.execute("UPDATE voice_capture_binding SET question_validation='stale'")
        else:
            c.execute("UPDATE voice_capture_binding SET wav_sha256=?", ('0' * 64,))
        c.commit()
    reopened = RubricEvidenceService(Database(db.path, media_root=media[4].parent))
    assert not reopened.qualification(CHILD, assessment['assessment_id'])['eligible_for_mastery_gate']
    with pytest.raises(ValueError):
        reopened.review(CHILD, assessment['assessment_id'], **{**body, 'expected_revision': 1, 'client_action_id': 'retry-changed'})
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 1


def test_later_turns_and_completed_session_do_not_erase_original_audio_receipt(media):
    db, service, assessment, body, prompt = prepared(media)
    service.review(CHILD, assessment['assessment_id'], **body)
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='completed'")
        c.execute("UPDATE agent_plan SET status='completed'")
        c.execute("UPDATE daily_session SET state='completed',status='completed',ended_at=datetime('now')")
        c.commit()
    reopened = RubricEvidenceService(Database(db.path, media_root=media[4].parent))
    assert reopened.qualification(CHILD, assessment['assessment_id'])['eligible_for_mastery_gate']


def test_direct_original_voice_uses_its_own_capture_not_a_confirmation(media):
    from test_pet_media_evidence import speaking, bind_speaking, local_review
    from home_study_agent.pet_speaking_review import prepare_speaking_audio_review
    prompt, text = speaking(media, transcript='我的答案是 I like reading in the library because it is quiet.')
    binding = bind_speaking(media, prompt)
    assessment = local_review(media, prompt, text, approve=False)
    with media[0].connect() as c:
        packet = prepare_speaking_audio_review(c, media[2], media_id=binding['media_id'],
            assessment_id=assessment['assessment_id'], media_root=media[4].parent)
    origin = packet['answer_voice_origin']
    assert origin['source_turn_id'] == origin['authorized_turn_id']
    assert origin['voice_input_id'] == origin['source_voice_input_id'] == 'voice'


def test_identical_text_and_audio_in_another_same_prompt_capture_are_not_the_answer(media):
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    from home_study_agent.pet_speaking_review import prepare_speaking_audio_review
    from test_pet_media_evidence import speaking, bind_speaking, local_review
    db, media_service, sid, _, path, _ = media
    prompt, text = speaking(media)
    original = bind_speaking(media, prompt)
    captures = VoiceCaptureBindingService(db)
    captures.begin(sid, 'second-voice-action', path.read_bytes(), 'pet_speaking',
                   {'child_id': CHILD, 'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}})
    with db.connect() as c:
        c.execute("""INSERT INTO voice_input SELECT 'second-voice',child_id,session_id,purpose,transcript,
            local_path,model_version,duration_ms,status,created_at,'second-voice-action' FROM voice_input WHERE voice_input_id='voice'""")
        c.execute("INSERT INTO voice_transcription_action VALUES('second-voice-action',?,?,'completed',?)",
                  (sid, CHILD, prompt['created_at']))
        c.commit()
    captures.complete(sid, 'second-voice-action', 'second-voice')
    different = media_service.bind_speaking(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        voice_input_id='second-voice', client_action_id='second-media')
    assert different['audio']['sha256'] == original['audio']['sha256']
    assessment = local_review(media, prompt, text, approve=False)
    args = dict(assessment_id=assessment['assessment_id'], media_root=path.parent)
    with db.connect() as c:
        assert prepare_speaking_audio_review(c, sid, media_id=original['media_id'], **args)['voice_input_id'] == 'voice'
        with pytest.raises(ValueError, match='ORIGINAL_VOICE_BINDING_REQUIRED'):
            prepare_speaking_audio_review(c, sid, media_id=different['media_id'], **args)

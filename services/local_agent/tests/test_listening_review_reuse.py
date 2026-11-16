"""Reuse reviewed instructional audio, never past playback or child speech."""
import json
import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.repository import StudyRepository
from home_study_agent.pet_media_evidence import project_pet_media_evidence
from test_pet_media_evidence import media, CHILD, review
from test_pet_scoped_listening import twice


def new_same_question(media, *, same_session=False):
    db, _, sid, original, path, _ = media
    old_audio = twice(media)
    DiagnosticPromptService(db).answer(sid, prompt_id=original['prompt_id'], item_version=original['item_version'],
        answer='in front of the library', client_action_id='old-original-answer')
    if not same_session:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
            c.commit()
        StudyRepository(db).complete_with_reflection(sid, '', '', '', '', False, 'reuse-end-original-day')
    reopened = Database(db.path, media_root=path.parent)
    next_sid = sid if same_session else StudyRepository(reopened).start_session(CHILD, 'reuse-next-session')['session_id']
    prompt = DiagnosticPromptService(reopened).prepare(next_sid, track='pet',
        preferred_item_id=original['diagnostic_item_id'], client_action_id='reuse-new-prompt')['prompt']
    return reopened, next_sid, prompt, old_audio


def test_new_prompt_reuses_original_approval_without_new_parent_fact_or_playback(media):
    from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
    db, sid, prompt, old = new_same_question(media)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bound = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        assert bound['status'] == 'ready', bound
        replay = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        assert replay == bound
        current = project_pet_media_evidence(c, sid, media_id=bound['media_id'], media_root=media[4].parent)
        assert current['source_reviewed'] and not current['qualification']['task_delivery_qualified']
        assert current['media_id'] != old['media_id'] and current['artifact_id'] == old['artifact_id']
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='review_source'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE media_id=? AND operation='playback'", (bound['media_id'],)).fetchone()[0] == 0
        c.commit()
    with Database(db.path, media_root=media[4].parent).connect() as c:
        restored = project_pet_media_evidence(c, sid, media_id=bound['media_id'], media_root=media[4].parent)
        assert restored['source_reviewed'] and not restored['qualification']['task_delivery_qualified']


@pytest.mark.parametrize('fault', ['approval', 'source', 'missing', 'bytes', 'owner'])
def test_reused_audio_rechecks_original_approval_and_artifact(media, fault):
    from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
    db, sid, prompt, old = new_same_question(media)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bound = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        c.commit()
    if fault == 'approval':
        review(media, old, 'retract', 'parent-withdraws-original-review')
    elif fault == 'missing':
        media[4].unlink()
    elif fault == 'bytes':
        media[4].write_bytes(b'changed synthetic audio')
    else:
        with db.connect() as c:
            if fault == 'source':
                c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id=?", (json.dumps({'changed': True}), old['artifact_id']))
            else:
                c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id=?", (old['artifact_id'],))
            c.commit()
    with db.connect() as c:
        current = project_pet_media_evidence(c, sid, media_id=bound['media_id'], media_root=media[4].parent)
        assert not current['source_reviewed'] and not current['qualification']['task_delivery_qualified'], current
    result = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='in front of the library', client_action_id='unverified-after-source-change')
    assert result['attempt']['score_status'] != 'verified_local'


def test_later_parent_reapproval_cannot_upgrade_old_reuse_reference(media):
    from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
    db, sid, prompt, old = new_same_question(media, same_session=True)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bound = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        c.commit()
    review(media, old, 'retract', 'retract-original')
    review(media, old, 'approve', 'new-explicit-original-approval')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        replay = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        assert replay['status'] == 'unavailable' and replay['media_id'] == bound['media_id']
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['future_approval', 'deleted_artifact', 'question_version'])
def test_reuse_rejects_future_approval_deleted_asset_and_changed_question(media, fault, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
    db, sid, prompt, old = new_same_question(media)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bound = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
        if fault == 'future_approval':
            approved_at = c.execute("SELECT created_at FROM pet_media_event WHERE media_id=? AND operation='review_source'",
                (old['media_id'],)).fetchone()[0]
            from home_study_agent import pet_media_evidence
            # Clock rollback must not make a future receipt usable. History stays immutable.
            monkeypatch.setattr(pet_media_evidence, '_now', lambda: datetime.fromisoformat(approved_at) - timedelta(seconds=1))
        elif fault == 'deleted_artifact':
            c.execute("UPDATE capture_artifact SET status='deleted' WHERE artifact_id=?", (old['artifact_id'],))
        else:
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' revised' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
        c.commit()
    with Database(db.path, media_root=media[4].parent).connect() as c:
        current = project_pet_media_evidence(c, sid, media_id=bound['media_id'], media_root=media[4].parent)
        assert not current['source_reviewed'] and not current['qualification']['task_delivery_qualified'], current


def test_repeated_reuse_always_pins_original_review_and_never_follows_a_chain(media):
    from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
    db, sid, prompt, old = new_same_question(media, same_session=True)
    media_ids = []
    for count in range(2):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            bound = bind_reviewed_listening_resource(c, sid, CHILD, prompt['prompt_id'], prompt['item_version'])
            assert bound['status'] == 'ready'
            state = json.loads(c.execute('SELECT state_json FROM pet_media_binding WHERE media_id=?', (bound['media_id'],)).fetchone()[0])
            assert state['source_reuse']['media_id'] == old['media_id']
            assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE media_id=? AND operation='playback'", (bound['media_id'],)).fetchone()[0] == 0
            c.commit()
        media_ids.append(bound['media_id'])
        if count == 0:
            service = DiagnosticPromptService(db)
            service.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                answer='in front of the library', client_action_id='unheard-second-reference')
            prompt = service.prepare(sid, track='pet', preferred_item_id=prompt['diagnostic_item_id'],
                client_action_id='third-prompt-not-second-source')['prompt']
    assert len(set(media_ids)) == 2
    review(media, old, 'retract', 'root-review-retracts-all-references')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='review_source'").fetchone()[0] == 2
        for media_id in media_ids:
            current = project_pet_media_evidence(c, sid, media_id=media_id, media_root=media[4].parent)
            assert not current['source_reviewed'] and not current['qualification']['task_delivery_qualified']

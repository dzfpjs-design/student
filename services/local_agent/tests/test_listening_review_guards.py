"""Exact generated review obligations retain their source and admission guards."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.pet_task_execution import resolve_pet_task_execution, is_listening_review_task
from home_study_agent.repository import StudyRepository, PlanInputError
from test_pet_media_evidence import media, CHILD, review
from test_pet_scoped_listening import twice


def generated_review(media):
    old = twice(media)
    db = Database(media[0].path, media_root=media[4].parent)
    sid, prompt = media[2:4]
    answer = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='football field', client_action_id='review-guard-original')
    assert answer['attempt']['score_status'] == 'verified_local'
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?', (CHILD,))
        c.commit()
    plan = repo.generate_plan(sid, 'guard-real-generated-plan')
    slot = next(x for x in plan['plan_items'] if x['source_type'] == 'due_review')
    with db.connect() as c:
        resolved = resolve_pet_task_execution(c, sid, slot['plan_item_id'])
    assert resolved['status'] == 'ready', resolved
    return db, sid, slot, resolved, old


@pytest.mark.parametrize('fault', ['item', 'classification', 'origin_approval', 'review_owner', 'due', 'snapshot'])
def test_bound_review_cannot_fall_back_to_unguarded_timer_or_new_item(media, fault):
    db, sid, slot, before, old_audio = generated_review(media)
    if fault == 'origin_approval':
        review(media, old_audio, 'retract', 'withdraw-original-listening-evidence')
    with db.connect() as c:
        if fault == 'item':
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (before['diagnostic_item_id'],))
        elif fault == 'classification':
            c.execute("UPDATE diagnostic_item SET answer_type='text',knowledge_node_id='node-pet-reading-main-idea' WHERE diagnostic_item_id=?", (before['diagnostic_item_id'],))
        elif fault == 'review_owner':
            c.execute("UPDATE review_task SET child_id='child-junior-3' WHERE review_task_id=?", (slot['source_ref'],))
        elif fault == 'due':
            c.execute('UPDATE review_task SET due_at=? WHERE review_task_id=?',
                ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), slot['source_ref']))
        elif fault == 'snapshot':
            c.execute("UPDATE plan_constraint_decision SET metadata_json=json_remove(metadata_json,'$.planning_evidence') WHERE source_ref=?", (slot['source_ref'],))
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
        current = resolve_pet_task_execution(c, sid, slot['plan_item_id'])
        assert current['status'] == 'blocked', current
        assert is_listening_review_task(c, {**slot, 'plan_item_id': slot['plan_item_id']})
    with pytest.raises(PlanInputError):
        StudyRepository(db).start_plan_item(slot['plan_item_id'], 'no-prompt-bypass')
    with db.connect() as c:
        assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (slot['plan_item_id'],)).fetchone()[0] is None
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (slot['source_ref'],)).fetchone()[0] == 0


def test_scheduling_does_not_grant_new_playback_or_change_other_child(media):
    db, sid, slot, resolved, _ = generated_review(media)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE child_id='child-junior-3'").fetchone()[0] == 0
        before = c.total_changes
        assert resolve_pet_task_execution(c, 'different-session', slot['plan_item_id'])['status'] == 'blocked'
        assert c.total_changes == before
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding WHERE diagnostic_item_id=?',
            (resolved['diagnostic_item_id'],)).fetchone()[0] == 0

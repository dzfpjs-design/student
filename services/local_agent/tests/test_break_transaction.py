"""Break lifecycle uses real plan writes and one owning transaction."""
import pytest

from home_study_agent.repository import StudyRepository, PlanInputError, PlanItemStateError
from home_study_agent.break_control import BreakControl
from test_learning_input_replan import schedule, inputs, flow


def test_begin_break_transition_failure_does_not_leave_task_paused(schedule, monkeypatch):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.start_plan_item('old', 'break-original-start')
    def fail(*args, **kwargs):
        raise OSError('transition fault')
    monkeypatch.setattr(repo, 'transition', fail)
    with pytest.raises(OSError, match='transition fault'):
        repo.begin_break(sid, 'break-fault')
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'active'
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'study'
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE client_action_id='break-fault:pause'").fetchone()[0] == 0


def test_resume_completion_failure_rolls_back_study_and_full_break_completion(schedule, monkeypatch):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET source_type='planned_break',homework_id=NULL,expected_minutes=5 WHERE plan_item_id='old'")
        c.commit()
    repo.begin_break(sid, 'begin-planned-break')
    original = repo.complete_plan_item
    def fail_after_completion(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('completion receipt fault')
    monkeypatch.setattr(repo, 'complete_plan_item', fail_after_completion)
    with pytest.raises(OSError, match='completion receipt fault'):
        repo.resume_after_break(sid, 'resume-break-fault')
    with db.connect() as c:
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'break'
        assert tuple(c.execute("SELECT status,actual_minutes,completed_at,completion_evidence_type FROM plan_item WHERE plan_item_id='old'").fetchone()) == ('active', None, None, None)
        assert c.execute("SELECT count(*) FROM session_transition WHERE client_action_id='resume-break-fault'").fetchone()[0] == 0


def test_explicit_borrowed_write_reads_uncommitted_state_without_committing(schedule):
    (db, _, _), _, _ = schedule
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert repo.start_plan_item('old', 'borrowed-start', connection=c)['status'] == 'active'
        assert c.in_transaction
        with db.connect() as observer:
            assert observer.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'pending'
        c.rollback()
    assert repo._get_plan_item_by_id('old')['status'] == 'pending'


def test_planned_break_uses_original_completion_fields_and_receipt_replay(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET source_type='planned_break',homework_id=NULL,expected_minutes=5 WHERE plan_item_id='old'")
        c.commit()
    repo.begin_break(sid, 'planned-break-begin')
    repo.resume_after_break(sid, 'planned-break-end')
    item = repo._get_plan_item_by_id('old')
    assert item['status'] == 'completed' and item['actual_minutes'] == 5
    assert item['completed_at'] and item['completion_evidence_type'] == 'planned_break'
    assert item['completion_note'] == '计划内休息已结束' and item['completion_checklist'] == []
    assert repo.resume_after_break(sid, 'planned-break-end')['replayed'] is True
    assert repo._get_plan_item_by_id('old') == item
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='completed' AND plan_item_id='old'").fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM break_control_receipt').fetchone()[0] == 2


def test_original_no_target_never_resumes_unrelated_paused_item(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.start_plan_item('old', 'unknown-paused-start')
    repo.pause_plan_item('old', 'unknown-paused-pause')
    repo.begin_break(sid, 'empty-break')
    repo.resume_after_break(sid, 'empty-break-end')
    assert repo._get_plan_item_by_id('old')['status'] == 'paused'
    with db.connect() as c:
        import json
        origin = json.loads(c.execute("SELECT binding_json FROM break_control_receipt WHERE client_action_id='empty-break'").fetchone()[0])
        assert origin['resume_item_id'] is None and origin['active_item'] is None


def test_resume_does_not_complete_the_next_unstarted_planned_break(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET source_type='planned_break',expected_minutes=5 WHERE plan_item_id='protected'")
        c.commit()
    repo.start_plan_item('old', 'before-next-break')
    repo.begin_break(sid, 'before-next-break-begin')
    repo.resume_after_break(sid, 'before-next-break-resume')
    assert repo._get_plan_item_by_id('old')['status'] == 'active'
    pending = repo._get_plan_item_by_id('protected')
    assert pending['status'] == 'pending' and pending['completed_at'] is None


@pytest.mark.parametrize('invalid', ['source', 'stage'])
def test_resume_original_source_failure_rolls_back_transition(schedule, invalid):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.start_plan_item('old', 'invalid-source-start')
    repo.begin_break(sid, 'invalid-source-break')
    with db.connect() as c:
        if invalid == 'source':
            c.execute("INSERT INTO plan_item_event VALUES ('deleted-source','old','revision_replanned',datetime('now'),'{}','deleted-source')")
        else:
            c.execute("UPDATE plan_item SET source_type='competition_math',source_ref='no-qualified-prompt' WHERE plan_item_id='old'")
        c.commit()
    with pytest.raises((PlanInputError, PlanItemStateError)):
        repo.resume_after_break(sid, 'invalid-source-resume')
    assert repo.get_session(sid)['state'] == 'break'
    assert repo._get_plan_item_by_id('old')['status'] == 'paused'
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM session_transition WHERE client_action_id='invalid-source-resume'").fetchone()
        assert not c.execute("SELECT 1 FROM break_control_receipt WHERE client_action_id='invalid-source-resume'").fetchone()


def test_replay_after_newer_actual_task_state_does_not_resume_again(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.start_plan_item('old', 'replay-start')
    repo.begin_break(sid, 'replay-begin')
    repo.resume_after_break(sid, 'replay-resume')
    repo.pause_plan_item('old', 'newer-explicit-pause')
    replay = StudyRepository(db).resume_after_break(sid, 'replay-resume')
    assert replay['replayed'] is True
    assert repo._get_plan_item_by_id('old')['status'] == 'paused'
    with pytest.raises(PlanInputError):
        repo.begin_break(sid, 'replay-resume')


def test_borrowed_connection_must_already_own_transaction(schedule):
    (db, _, _), _, _ = schedule
    with db.connect() as c, pytest.raises(ValueError, match='active transaction'):
        StudyRepository(db).start_plan_item('old', 'no-transaction', connection=c)


def test_legacy_break_without_binding_is_not_permission_to_resume_paused_task(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.start_plan_item('old', 'legacy-start')
    repo.pause_plan_item('old', 'legacy-pause')
    repo.transition(sid, 'study', 'break', 'child_requested_break', 'legacy-break-transition')
    with pytest.raises(PlanInputError, match='核对原任务'):
        repo.resume_after_break(sid, 'new-legacy-resume')
    assert repo.get_session(sid)['state'] == 'break'
    assert repo._get_plan_item_by_id('old')['status'] == 'paused'
    replay = repo.begin_break(sid, 'legacy-break-transition')
    assert replay['replayed'] and replay['break_receipt']['status'] == 'legacy_recorded'
    assert repo.get_session(sid)['state'] == 'break'


def test_receipt_key_cannot_cross_session_or_child(schedule):
    (db, sid, _), _, _ = schedule
    repo = StudyRepository(db)
    repo.begin_break(sid, 'one-child-break')
    with db.connect() as c:
        # The product permits only one active family session at a time.
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.execute('''INSERT INTO daily_session(session_id,child_id,state,status,started_at,last_checkpoint_at,client_action_id)
            VALUES ('other-break-session','child-junior-3','study','active',datetime('now'),datetime('now'),'other-break-session')''')
        c.commit()
    with pytest.raises(PlanInputError):
        repo.begin_break('other-break-session', 'one-child-break')
    assert repo.get_session('other-break-session')['state'] == 'study'

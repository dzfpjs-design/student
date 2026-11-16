"""W24 isolated SQLite: process lifecycle is not a child outcome."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import subprocess
import sys

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.companion_activity_outcome import (
    CompanionActivityOutcomeService, ActivityConflict, ActivityInputError, UnknownActivity,
)
from home_study_agent.database import Database
from test_controlled_game import seed


@pytest.fixture
def activity(tmp_path):
    db = Database(tmp_path / 'isolated.db')
    db.initialize()
    session, item = seed(db, tmp_path / 'synthetic.exe')
    runtime = AgentRuntimeStore(db)
    source = runtime.start_companion_activity(session, 'movement', 'Stretch gently', 'start', item)
    with db.connect() as c:
        c.execute("INSERT INTO child_profile VALUES ('other','Other','9','Nanjing','active',?,?)", ('now', 'now'))
        c.commit()
    service = CompanionActivityOutcomeService(db)
    receipt = registered_outcome(service, source['activity_id'])
    return db, runtime, service, receipt['activity']['outcome_id'], session, item


def registered_outcome(service, source_id):
    with service.database.connect() as c:
        oid = c.execute("SELECT outcome_id FROM companion_activity_outcome WHERE child_id='child-game' AND source_kind='companion' AND source_id=?",
                        (source_id,)).fetchone()
    assert oid is not None, 'Runtime must atomically register its new activity'
    return service.get('child-game', oid[0])


def legacy_activity(db, session, item):
    """A pre-W24 activity, deliberately without a registered outcome."""
    with db.connect() as c:
        c.execute('''INSERT INTO companion_activity(activity_id,child_id,session_id,activity_type,topic,status,
            turn_count,max_turns,duration_minutes,paused_plan_item_id,started_at,ends_at,updated_at,client_action_id)
            VALUES ('legacy-activity','child-game',?,'tidy_challenge','Put pencils away','active',0,2,5,?,
            datetime('now'),datetime('now','+5 minutes'),datetime('now'),'legacy-start')''', (session,item))
        c.commit()
    return 'legacy-activity'


def feedback(service, oid, revision=1, key='feedback', **overrides):
    return service.feedback('child-game', oid, expected_revision=revision, client_action_id=key,
        **dict(dict(execution='completed', experience='comfortable', note='I stretched my shoulders',
                    evidence_basis='child_report', actor='child'), **overrides))


def test_timer_is_not_execution_or_return_and_feedback_survives_reopen(activity):
    db, runtime, s, oid, session, _ = activity
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET ends_at='2000-01-01T00:00:00+00:00'")
        c.commit()
    runtime.active_companion_activity(session)
    view = s.get('child-game', oid)['activity']
    assert view['lifecycle']['ended'] is True
    assert view['execution_status'] == 'unknown'
    assert view['resume']['status'] == 'pending'
    assert view['next_action']['kind'] == 'ask_feedback'
    assert not view['habit_evidence_eligible']
    receipt = feedback(s, oid)
    assert receipt['activity']['execution_status'] == 'reported_completed'
    assert receipt['activity']['next_action']['kind'] == 'offer_resume'
    reopened = CompanionActivityOutcomeService(Database(db.path))
    assert reopened.get('child-game', oid) == s.get('child-game', oid)
    retracted = reopened.retract('child-game', oid, expected_revision=2, client_action_id='retract',
                                target_revision=2, reason='Actually I did not leave the desk', actor='parent')
    assert retracted['activity']['execution_status'] == 'unknown'
    replay = feedback(reopened, oid)
    assert replay['event'] == receipt['event']
    assert replay['activity']['revision'] == 3
    assert replay['activity']['execution_status'] == 'unknown'


@pytest.mark.parametrize('execution,experience', [('refused','unknown'), ('stopped','comfortable'),
    ('not_executed','comfortable'), ('completed','fatigued'), ('completed','uncomfortable')])
def test_refusal_stop_fatigue_never_habit_success(activity, execution, experience):
    _, runtime, s, oid, session, _ = activity
    runtime.end_companion_activity(session, 'child_requested')
    result = feedback(s, oid, execution=execution, experience=experience,
                      evidence_basis='parent_observed', actor='parent')['activity']
    assert not result['habit_evidence_eligible']
    assert result['next_action']['kind'] == 'offer_rest_or_defer'


def test_observed_completion_requires_parent_and_concrete_feedback(activity):
    _, runtime, s, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    with pytest.raises(ActivityInputError):
        feedback(s, oid, evidence_basis='parent_observed')
    with pytest.raises(ActivityInputError):
        feedback(s, oid, note=' ')
    result = feedback(s, oid, evidence_basis='parent_observed', actor='parent')['activity']
    assert result['execution_status'] == 'observed_completed'
    assert result['habit_evidence_eligible'] is False
    assert 'W20_OBSERVATION_QUALIFICATION_REQUIRED' in result['evidence_limits']


def test_resume_needs_request_and_exact_persisted_executor_receipt(activity):
    db, runtime, s, oid, session, item = activity
    runtime.end_companion_activity(session, 'turn_limit')
    feedback(s, oid)
    requested = s.request_resume('child-game', oid, expected_revision=2, client_action_id='resume',
                                 note='Ready to continue', actor='child')
    req = requested['event']['event_id']
    def confirm(key='confirm'):
        return s.confirm_resume('child-game', oid, expected_revision=3, client_action_id=key,
                                 request_event_id=req, plan_item_event_id='resumed')
    with pytest.raises(ActivityConflict):
        confirm()
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='active' WHERE plan_item_id=?", (item,))
        c.execute("INSERT INTO plan_item_event VALUES ('resumed',?,'resumed',?,?, 'resume-executor')",
                  (item, datetime.now(timezone.utc).isoformat(), json.dumps({'reason':'time_limit'})))
        c.commit()
    with pytest.raises(ActivityConflict):
        confirm()
    with db.connect() as c:
        c.execute("UPDATE plan_item_event SET payload_json=? WHERE event_id='resumed'",
                  (json.dumps({'companion_outcome_id':oid, 'resume_request_event_id':req}),))
        c.commit()
    result = confirm()['activity']
    assert result['resume']['status'] == 'confirmed'
    assert result['next_action']['kind'] == 'continue_original'
    assert result['resume']['original']['plan_item_id'] == item
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (session,))
        c.commit()
    closed = s.get('child-game', oid)['activity']
    assert closed['resume']['status'] == 'original_unavailable'
    assert closed['next_action']['kind'] == 'resolve_original'
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='active' WHERE session_id=?", (session,))
        c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id=?", (item,))
        c.commit()
    assert s.get('child-game', oid)['activity']['next_action']['kind'] == 'resolve_original'
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='active' WHERE plan_item_id=?", (item,))
        c.execute("UPDATE companion_activity SET topic='Replaced source'")
        c.commit()
    assert s.get('child-game', oid)['activity']['resume']['status'] == 'source_invalid'
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET topic='Stretch gently'")
        c.commit()
    with db.connect() as c:
        c.execute("DELETE FROM plan_item_event WHERE event_id='resumed'")
        c.commit()
    assert s.get('child-game', oid)['activity']['resume']['status'] == 'receipt_invalid'


def test_source_revision_and_child_isolation_fail_closed(activity):
    db, runtime, s, oid, session, _ = activity
    with pytest.raises(UnknownActivity):
        s.get('other', oid)
    with pytest.raises(UnknownActivity):
        s.open('other', source_kind='companion', source_id=runtime.active_companion_activity(session)['activity_id'],
               intent='x', success_description='y', client_action_id='open')
    feedback(s, oid)
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET topic='Different activity'")
        c.commit()
    assert s.get('child-game', oid)['activity']['execution_status'] == 'source_invalid'
    with pytest.raises(ActivityConflict):
        feedback(s, oid, revision=2, key='changed')
    s.retract('child-game', oid, expected_revision=2, client_action_id='retract', target_revision=2,
              reason='Source changed', actor='parent')


def test_idempotency_concurrency_and_no_silent_revision_overwrite(activity):
    _, _, s, oid, _, _ = activity
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: feedback(s, oid), range(2)))
    assert receipts[0]['event'] == receipts[1]['event']
    with pytest.raises(ActivityConflict):
        feedback(s, oid, note='Different body')
    with pytest.raises(ActivityConflict):
        feedback(s, oid, key='stale')
    s.defer_resume('child-game', oid, expected_revision=2, client_action_id='defer',
                   note='Still tired; return later', actor='child')
    assert s.get('child-game', oid)['activity']['resume']['status'] == 'deferred'
    assert len(s.get('child-game', oid)['history']) == 3


def test_scoped_privacy_hard_delete_not_blocked(activity):
    db, _, s, oid, _, _ = activity
    feedback(s, oid)
    with db.connect() as c:
        c.execute('DELETE FROM companion_activity_outcome WHERE outcome_id=?', (oid,))
        c.commit()
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome_event').fetchone()[0] == 0


def test_new_fatigue_feedback_invalidates_pending_resume_request(activity):
    _, runtime, s, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    feedback(s, oid)
    s.request_resume('child-game', oid, expected_revision=2, client_action_id='resume', note='Ready', actor='child')
    view = feedback(s, oid, revision=3, key='tired-now', experience='fatigued')['activity']
    assert view['resume']['status'] == 'needs_reconfirmation'
    assert view['next_action']['kind'] == 'offer_rest_or_defer'
    view = s.request_resume('child-game', oid, expected_revision=4, client_action_id='resume-again',
                             note='After resting, I want to continue', actor='child')['activity']
    assert view['next_action']['kind'] == 'resume_original'


def test_original_plan_repurposed_or_session_closed_never_continues(activity):
    db, runtime, s, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    feedback(s, oid)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET title='A different assignment' WHERE plan_item_id='item-game'")
        c.commit()
    assert s.get('child-game', oid)['activity']['source_valid'] is False
    with pytest.raises(ActivityConflict):
        s.request_resume('child-game', oid, expected_revision=2, client_action_id='wrong-task', note='Ready', actor='child')


def test_fresh_process_reads_same_version_without_private_runtime_state(activity):
    db, _, s, oid, _, _ = activity
    feedback(s, oid)
    script = '''import json,sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
print(json.dumps(CompanionActivityOutcomeService(Database(Path(sys.argv[1]))).get('child-game',sys.argv[2])))
'''
    result = subprocess.run([sys.executable, '-c', script, str(db.path), oid], capture_output=True, text=True,
                            timeout=20, check=True)
    assert json.loads(result.stdout) == s.get('child-game', oid)


def test_unbound_original_is_not_claimed_as_no_need_to_return(activity):
    _, runtime, s, _, session, _ = activity
    source = runtime.start_companion_activity(session, 'riddle', 'Synthetic riddle', 'unbound')
    oid = registered_outcome(s, source['activity_id'])['activity']['outcome_id']
    runtime.end_companion_activity(session, 'turn_limit')
    view = feedback(s, oid, key='unbound-feedback')['activity']
    assert view['resume']['status'] == 'no_original_bound'
    assert view['next_action']['kind'] == 'clarify_original'


@pytest.mark.parametrize('kind', ['interactive_story','word_chain','riddle','movement','tidy_challenge'])
def test_all_existing_companion_types_keep_outcome_unknown_at_lifecycle_end(activity, kind):
    _, runtime, s, _, session, item = activity
    source = runtime.start_companion_activity(session, kind, 'Synthetic activity', 'start-' + kind, item)
    opened = registered_outcome(s, source['activity_id'])
    runtime.end_companion_activity(session, 'turn_limit')
    result = s.get('child-game', opened['activity']['outcome_id'])['activity']
    assert result['execution_status'] == 'unknown'
    assert result['resume']['status'] == 'pending'


@pytest.mark.parametrize('kind,reason', [('controlled_game','process_exited'), ('controlled_content','media_ended')])
def test_real_controlled_services_do_not_certify_execution_from_process_end(tmp_path, kind, reason):
    from home_study_agent.controlled_game import ControlledGameService
    from home_study_agent.controlled_content import ControlledContentService
    from test_controlled_content import seed as content_seed
    db = Database(tmp_path / 'controlled.db')
    db.initialize()
    if kind == 'controlled_game':
        session, item = seed(db, tmp_path / 'game.exe')
        child = 'child-game'
        driver = ControlledGameService(db)
        driver.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        driver.approve(child, 'Game', str(tmp_path / 'game.exe'), 5)
        source = driver.start(session, 'Game', 'controlled-start', item)
        source_id = source['game_session_id']
    else:
        session, resource = content_seed(db, tmp_path / 'videos')
        child, item = 'child-media', 'item-media'
        driver = ControlledContentService(db)
        driver.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        driver.approve(child, resource, 5)
        source = driver.start(session, '分数', 'controlled-start', item)
        source_id = source['content_session_id']
    s = CompanionActivityOutcomeService(db)
    with db.connect() as c:
        oid = c.execute('SELECT outcome_id FROM companion_activity_outcome WHERE child_id=? AND source_kind=? AND source_id=?',
                        (child,kind,source_id)).fetchone()[0]
    identity_key = 'game_session_id' if kind == 'controlled_game' else 'content_session_id'
    driver.end(session, reason, **{identity_key: source_id})
    result = CompanionActivityOutcomeService(Database(db.path)).get(child, oid)
    assert result['activity']['lifecycle']['ended']
    assert result['activity']['execution_status'] == 'unknown'
    assert result['activity']['resume']['status'] == 'pending'
    assert str(tmp_path) not in json.dumps(result)

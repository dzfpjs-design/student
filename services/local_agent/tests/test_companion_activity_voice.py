"""Real runtime plan persistence to W24 self-report journal, without cloud calls."""
import json

import pytest

from home_study_agent.agent_runtime import CapabilityRegistry, CapabilitySpec, parse_agent_plan_output, InvalidAgentPlanError
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService, ActivityVoiceError
from home_study_agent.companion_activity_outcome import ActivityConflict, CompanionActivityOutcomeService
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_companion_activity_outcome import activity


def voice_turn(activity, key='voice', operation='feedback', quote='我只做了两次，现在有点累。', **changes):
    db, runtime, _, oid, session, _ = activity
    arguments = dict(operation=operation, response_quote=quote)
    if operation == 'feedback':
        arguments.update(execution='partial', experience='fatigued')
    arguments.update(changes.pop('arguments', {}))
    binding = dict(outcome_id=oid, expected_revision=1, child_id='child-game', session_id=session)
    binding.update(changes.pop('binding', {}))
    registry = CapabilityRegistry((CapabilitySpec('companion.activity_feedback', '1', 'Persist self-report',
        ('companion', 'execution'), (), 'server', 'low', ('operation','response_quote'),
        ('activity_self_report_recorded',), 'record_companion_activity_feedback'),))
    decision = parse_agent_plan_output(json.dumps(dict(objective={'mode':'none'},
        next_step={'capability':'companion.activity_feedback','arguments':arguments},
        spoken_transition='先确认实际情况。', requires_child_reply=False, confidence=0.9)), 'study', registry)
    with db.connect() as c:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,tool_status,created_at,client_action_id)
            VALUES (?,'child-game',?,?,'companion','',?,'synthetic','pending',datetime('now'),?)''',
            (key, session, quote, json.dumps({'type':'record_companion_activity_feedback'}), key))
        plan = runtime.persist_plan(c, child_id='child-game', session_id=session, agent_turn_id=key,
            client_action_id=key, decision=decision, context_manifest={'companion_activity_outcome_binding':binding})
        c.execute("UPDATE agent_plan SET status='running' WHERE plan_id=?", (plan['plan_id'],))
        c.execute("UPDATE agent_step SET status='running' WHERE plan_id=?", (plan['plan_id'],))
        c.commit()
    return key


def test_exact_original_voice_is_always_self_report_persisted_and_replayable(activity):
    db, _, book, oid, _, _ = activity
    turn = voice_turn(activity)
    service = CompanionActivityVoiceService(db)
    result = service.execute(turn)
    report = result['activity']['feedback']['payload']
    assert report['agent_turn_id'] == turn
    assert report['response_quote'] == '我只做了两次，现在有点累。'
    assert report['note'] == report['response_quote']
    assert report['evidence_basis'] == 'child_report' and report['report_kind'] == 'self_report'
    assert result['activity']['next_action']['kind'] == 'stop_activity'
    assert not result['activity']['habit_evidence_eligible']
    assert result['evidence'] == ['activity_self_report_recorded']
    reopened = CompanionActivityVoiceService(Database(db.path))
    assert reopened.execute(turn)['event'] == result['event']
    book.retract('child-game', oid, expected_revision=2, client_action_id='retract-voice',
                  target_revision=2, reason='Correcting self report', actor='child')
    replay = reopened.execute(turn)
    assert replay['event'] == result['event']
    assert replay['activity']['execution_status'] == 'unknown'
    assert replay['activity']['revision'] == 3
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM companion_activity_voice_binding').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    {'arguments':{'response_quote':'孩子做完了'}},
    {'arguments':{'response_quote':'我只做了两次，现在有点累。 '}},
    {'arguments':{'actor':'parent'}},
    {'arguments':{'evidence_basis':'parent_observed'}},
    {'arguments':{'outcome_id':'model-chosen'}},
    {'binding':{'child_id':'other'}},
    {'binding':{'session_id':'another-session'}},
    {'binding':{'expected_revision':True}},
    {'binding':{'expected_revision':2}},
])
def test_model_fabrication_identity_and_stale_version_fail_closed(activity, change):
    db, _, book, oid, _, _ = activity
    with pytest.raises((ActivityVoiceError, ActivityConflict, InvalidAgentPlanError)):
        turn = voice_turn(activity, **change)
        CompanionActivityVoiceService(db).execute(turn)
    assert book.get('child-game', oid)['activity']['revision'] == 1


@pytest.mark.parametrize('change', ['transcript','arguments','manifest','cancel','child','session_closed'])
def test_frozen_binding_cannot_be_retargeted_or_run_after_cancellation(activity, change):
    db, _, book, oid, session, _ = activity
    turn = voice_turn(activity)
    service = CompanionActivityVoiceService(db)
    service.bind_turn(turn)
    with db.connect() as c:
        if change == 'transcript':
            c.execute("UPDATE agent_turn SET transcript='修订后的另一句话' WHERE agent_turn_id=?", (turn,))
        elif change == 'arguments':
            c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.execution','completed')")
        elif change == 'manifest':
            c.execute("UPDATE agent_plan SET context_manifest_json='{}'")
        elif change == 'cancel':
            c.execute("UPDATE agent_plan SET status='cancelled'")
        elif change == 'child':
            c.execute("UPDATE agent_turn SET child_id='other' WHERE agent_turn_id=?", (turn,))
        else:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (session,))
        c.commit()
    with pytest.raises(ActivityVoiceError):
        service.execute(turn)
    assert book.get('child-game', oid)['activity']['revision'] == 1


def test_revised_source_turn_invalidates_historical_report_and_resume_intent(activity):
    db, runtime, book, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, quote='我做完了。', arguments={'execution':'completed','experience':'comfortable'})
    service = CompanionActivityVoiceService(db)
    assert service.execute(turn)['activity']['execution_status'] == 'reported_completed'
    resume = voice_turn(activity, key='resume-voice', operation='request_resume', quote='现在继续刚才的数学。',
                         binding={'expected_revision':2})
    assert service.execute(resume)['activity']['resume']['status'] == 'requested'
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript='我没有做。' WHERE agent_turn_id=?", (turn,))
        c.execute("UPDATE agent_turn SET transcript='还不想继续。' WHERE agent_turn_id=?", (resume,))
        c.commit()
    view = book.get('child-game', oid)['activity']
    assert view['execution_status'] == 'unknown'
    assert view['resume']['status'] == 'needs_reconfirmation'
    assert len(book.get('child-game', oid)['history']) == 3


def test_decline_records_actual_local_stop_without_resuming_original_timer(activity):
    db, runtime, _, _, session, item = activity
    result = CompanionActivityVoiceService(db).execute(voice_turn(activity, operation='decline', quote='不想做了。'))
    assert result['activity']['feedback']['payload']['execution'] == 'refused'
    assert result['activity']['next_action']['kind'] == 'offer_rest_or_defer'
    assert result['activity_stop']['status'] == 'stopped'
    assert result['event']['payload']['activity_stop'] == result['activity_stop']
    assert 'companion_activity_stopped' in result['evidence']
    assert runtime.active_companion_activity(session) is None
    assert not result['activity']['habit_evidence_eligible']
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(item,)).fetchone()[0] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


def test_original_activity_is_used_even_when_another_one_started(activity):
    db, runtime, _, oid, session, _ = activity
    turn = voice_turn(activity)
    service = CompanionActivityVoiceService(db)
    service.bind_turn(turn)
    new = runtime.start_companion_activity(session, 'riddle', 'Another activity', 'another')
    result = service.execute(turn)
    assert result['activity']['outcome_id'] == oid
    assert result['activity']['source_id'] != new['activity_id']


def test_privacy_can_delete_voice_binding_without_immutable_delete_barrier(activity):
    db, _, book, oid, _, _ = activity
    CompanionActivityVoiceService(db).execute(voice_turn(activity))
    with db.connect() as c:
        c.execute('DELETE FROM companion_activity_outcome WHERE outcome_id=?', (oid,))
        c.commit()
        assert c.execute('SELECT COUNT(*) FROM companion_activity_voice_binding').fetchone()[0] == 0


def test_explicit_resume_uses_real_repository_event_not_automatic_or_random_receipt(activity):
    db, runtime, book, oid, session, item = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, operation='request_resume', quote='我现在想继续刚才的数学。')
    result = CompanionActivityVoiceService(db).execute(turn)
    assert result['activity']['resume']['status'] == 'requested'
    key = result['activity']['resume']['execution_key']
    request = result['event']['event_id']
    repository = StudyRepository(db)
    repository.resume_plan_item(item, key)
    with db.connect() as c:
        event = c.execute('SELECT * FROM plan_item_event WHERE client_action_id=?',(key,)).fetchone()
        assert json.loads(event['payload_json']) == {}
    acknowledged = book.confirm_resume('child-game', oid, expected_revision=2, client_action_id='voice-ack',
        request_event_id=request, plan_item_event_id=event['event_id'])
    assert acknowledged['activity']['resume']['status'] == 'confirmed'
    assert not acknowledged['activity']['resume']['actual_child_return_verified']
    assert not acknowledged['activity']['habit_evidence_eligible']
    restored = CompanionActivityOutcomeService(Database(db.path)).get('child-game', oid)
    assert restored['activity']['resume']['status'] == 'confirmed'
    book.retract('child-game', oid, expected_revision=3, client_action_id='retract-intent', target_revision=2,
                  reason='Withdraw return intent', actor='child')
    assert book.get('child-game', oid)['activity']['resume']['status'] != 'confirmed'


def test_other_running_activity_blocks_original_task_resume(activity):
    db, runtime, book, _, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, operation='request_resume', quote='我现在想继续刚才的数学。')
    runtime.start_companion_activity(session, 'riddle', 'Another activity', 'new-active')
    with pytest.raises(ActivityConflict, match='OTHER_ACTIVITY_ACTIVE'):
        CompanionActivityVoiceService(db).execute(turn)


def test_new_fatigue_after_bound_resume_intent_requires_new_voice_request(activity):
    db, runtime, book, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    service = CompanionActivityVoiceService(db)
    service.execute(voice_turn(activity, operation='request_resume', quote='现在继续数学。'))
    result = service.execute(voice_turn(activity, key='tired-after-request', binding={'expected_revision':2}))
    assert result['activity']['resume']['status'] == 'needs_reconfirmation'
    assert result['activity']['resume']['execution_key'] is None


def test_atomic_resume_rolls_back_real_plan_event_on_confirmation_failure(activity, monkeypatch):
    db, runtime, book, oid, session, item = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, operation='request_resume', quote='现在继续数学。')
    service = CompanionActivityVoiceService(db)
    service.execute(turn)
    append = service.outcomes._append
    def fail_confirm(*args, **kwargs):
        if args[3] == 'resume_confirmed':
            raise RuntimeError('isolated confirmation write failed')
        return append(*args, **kwargs)
    monkeypatch.setattr(service.outcomes, '_append', fail_confirm)
    with pytest.raises(RuntimeError, match='confirmation write failed'):
        service.resume_original(turn)
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(item,)).fetchone()[0] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
    assert book.get('child-game', oid)['activity']['revision'] == 2
    monkeypatch.setattr(service.outcomes, '_append', append)
    done = service.resume_original(turn)
    assert done['activity']['resume']['status'] == 'confirmed'
    replay = CompanionActivityVoiceService(Database(db.path)).resume_and_confirm(turn)
    assert replay['event'] == done['event']
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
        assert c.execute("SELECT status FROM agent_step WHERE step_index=0").fetchone()[0] == 'running'
        assert c.execute("SELECT tool_status FROM agent_turn WHERE agent_turn_id=?", (turn,)).fetchone()[0] == 'pending'


@pytest.mark.parametrize('condition', ['key_only','already_active','other_active','new_feedback'])
def test_atomic_resume_key_is_never_authority(activity, condition):
    db, runtime, book, oid, session, item = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, operation='request_resume', quote='现在继续数学。')
    service = CompanionActivityVoiceService(db)
    request = service.execute(turn)
    with db.connect() as c:
        c.execute('UPDATE plan_item SET active_seconds=73,active_segment_started_at=NULL WHERE plan_item_id=?',(item,))
        if condition == 'key_only':
            c.execute("INSERT INTO plan_item_event VALUES ('forged-key',?,'resumed',datetime('now'),'{}',?)",
                      (item, request['activity']['resume']['execution_key']))
        elif condition == 'already_active':
            c.execute("UPDATE plan_item SET status='active' WHERE plan_item_id=?",(item,))
        elif condition == 'other_active':
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,source_type,source_ref,task_type) VALUES ('other-task','plan-game','英语','Another',10,2,'active',2,'homework','other','written')''')
        c.commit()
    if condition == 'new_feedback':
        book.feedback('child-game', oid, execution='not_executed', experience='fatigued', note='Not ready',
            evidence_basis='child_report', actor='child', expected_revision=2, client_action_id='new-feedback')
    with pytest.raises(ActivityConflict):
        service.resume_original(turn)
    with db.connect() as c:
        timer = c.execute('SELECT active_seconds,active_segment_started_at FROM plan_item WHERE plan_item_id=?',(item,)).fetchone()
        assert tuple(timer) == (73, None)
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='resume_confirmed'").fetchone()[0] == 0


def test_atomic_resume_rechecks_cancelled_or_revised_voice_authorization(activity):
    db, runtime, book, oid, session, item = activity
    runtime.end_companion_activity(session, 'turn_limit')
    turn = voice_turn(activity, operation='request_resume', quote='现在继续数学。')
    service = CompanionActivityVoiceService(db)
    service.execute(turn)
    with db.connect() as c:
        c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(turn,))
        c.commit()
    with pytest.raises(ActivityVoiceError):
        service.resume_original(turn)
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(item,)).fetchone()[0] == 'paused'

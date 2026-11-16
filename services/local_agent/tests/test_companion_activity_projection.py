"""Read-only history qualification, separate from current resume availability."""
import json

from home_study_agent.companion_activity_outcome import project_companion_activity_evidence
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from test_companion_activity_outcome import activity
from test_companion_activity_voice import voice_turn


def project(db, child='child-game'):
    with db.connect() as c:
        c.execute('BEGIN')
        before = c.total_changes
        result = project_companion_activity_evidence(c, child, '2000-01-01')
        assert c.total_changes == before
        return result


def confirmed(activity):
    db,runtime,book,oid,session,item=activity
    runtime.end_companion_activity(session,'turn_limit')
    turn=voice_turn(activity,operation='request_resume',quote='现在继续数学。')
    voice=CompanionActivityVoiceService(db)
    voice.execute(turn)
    done=voice.resume_original(turn)
    return done,turn


def test_later_task_completion_and_session_end_do_not_erase_historical_receipt(activity):
    db,_,_,_,session,item=activity
    done,turn=confirmed(activity)
    first=project(db)
    assert first['task_resume_receipt_count']==1
    assert first['activities'][0]['historical_resume_confirmed']
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id=?",(item,))
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?",(session,))
        c.commit()
    later=project(db)
    assert later['task_resume_receipt_count']==1
    assert later['activities'][0]['current_resume_status']=='original_unavailable'
    assert later['verified_child_return_count']==0 and later['habit_success_count']==0
    assert project(db,'other')['activities']==[]


def test_retraction_and_voice_revision_change_qualification_and_fingerprint(activity):
    db,_,book,oid,_,_=activity
    done,turn=confirmed(activity)
    before=project(db)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript='先不继续。' WHERE agent_turn_id=?",(turn,))
        c.commit()
    revised=project(db)
    assert revised['task_resume_receipt_count']==0
    assert revised['fingerprint']!=before['fingerprint']
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript='现在继续数学。' WHERE agent_turn_id=?",(turn,))
        c.commit()
    book.retract('child-game',oid,expected_revision=3,client_action_id='withdraw-request',
                 target_revision=2,reason='Withdraw intent',actor='child')
    assert project(db)['task_resume_receipt_count']==0


def test_source_revision_and_receipt_deletion_fail_closed(activity):
    db,_,_,_,_,_=activity
    done,_=confirmed(activity)
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET topic='Different source'")
        c.commit()
    assert project(db)['task_resume_receipt_count']==0
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET topic='Stretch gently'")
        c.execute('DELETE FROM plan_item_event WHERE event_id=?',(done['event']['payload']['plan_item_event_id'],))
        c.commit()
    assert project(db)['task_resume_receipt_count']==0


def test_self_report_is_counted_separately_without_raw_quote_or_habit_success(activity):
    db,_,_,_,_,_=activity
    quote='我只做了两次，现在有点累。'
    CompanionActivityVoiceService(db).execute(voice_turn(activity,quote=quote))
    result=project(db)
    assert result['self_report_count']==1 and result['parent_observed_count']==0
    assert result['task_resume_receipt_count']==result['habit_success_count']==0
    assert quote not in json.dumps(result,ensure_ascii=False)


def test_empty_historical_database_is_not_migrated_by_read_projection(activity):
    db,_,_,_,_,_=activity
    with db.connect() as c:
        c.execute('DROP TABLE companion_activity_outcome_event')
        c.execute('DROP TABLE companion_activity_outcome')
        c.commit()
    result=project(db)
    assert result['activities']==[] and result['task_resume_receipt_count']==0
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='companion_activity_outcome'").fetchone() is None


def test_fingerprint_ignores_moving_cutoff_but_tracks_two_invalid_source_revisions(activity):
    db,_,_,_,_,_=activity
    with db.connect() as c:
        a=project_companion_activity_evidence(c,'child-game','2000-01-01T00:00:00.000001')
        b=project_companion_activity_evidence(c,'child-game','2000-01-01T00:00:00.000002')
        assert a['fingerprint']==b['fingerprint']
        c.execute("UPDATE companion_activity SET topic='Changed source 1'"); c.commit()
    first=project(db)
    with db.connect() as c:
        c.execute("UPDATE companion_activity SET topic='Changed source 2'"); c.commit()
    second=project(db)
    assert not first['activities'][0]['source_valid'] and not second['activities'][0]['source_valid']
    assert first['fingerprint']!=second['fingerprint']


def test_fingerprint_tracks_multiple_invalid_utterance_revisions(activity):
    db,_,_,_,_,_=activity
    _,turn=confirmed(activity)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript='Changed utterance 1' WHERE agent_turn_id=?",(turn,)); c.commit()
    first=project(db)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET transcript='Changed utterance 2' WHERE agent_turn_id=?",(turn,)); c.commit()
    second=project(db)
    assert first['task_resume_receipt_count']==second['task_resume_receipt_count']==0
    assert first['fingerprint']!=second['fingerprint']

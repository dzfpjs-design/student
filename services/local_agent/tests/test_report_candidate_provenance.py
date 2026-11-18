"""A historical unconfirmed composite must not become report evidence."""
import pytest
from test_report_source_fingerprint import db, SID, CHILD, insert
from home_study_agent.report_source_fingerprint import report_source_fingerprint
from home_study_agent.daily_report_frozen_dependencies import _hash, valid
from home_study_agent.repository import StudyRepository, ReportNotReadyError


def _artifact(c, kind='document_ocr'):
    insert(c, 'capture_artifact', artifact_id='candidate-test', child_id=CHILD, session_id=SID,
           artifact_type=kind, status='completed', details_json='{}', created_at='2026-01-01',
           client_action_id='candidate-test')


@pytest.mark.parametrize('reference', ['completion', 'homework'])
def test_old_unconfirmed_reference_blocks_report_without_deleting_records_and_invalidates_frozen(db, reference):
    with db.connect() as c:
        _artifact(c)
        if reference == 'completion':
            c.execute("UPDATE plan_item SET completion_artifact_id='candidate-test' WHERE plan_item_id='item'")
        else:
            insert(c,'homework_item',homework_id='hw',child_id=CHILD,session_id=SID,subject='数学',
                   task_type='written',title='合成作业',expected_minutes=20,source_artifact_id='candidate-test',
                   confirmation_status='child_confirmed',status='active',created_at='2026-01-01',client_action_id='hw')
            c.execute("UPDATE plan_item SET homework_id='hw' WHERE plan_item_id='item'")
        c.commit()
        before=report_source_fingerprint(c,SID)
        c.execute('BEGIN'); digest=_hash(c,CHILD,'task','item'); assert valid(c,CHILD,[('task','item',digest)]); c.rollback()
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='candidate-test'")
        c.commit()
        assert report_source_fingerprint(c,SID) != before
        c.execute('BEGIN'); assert not valid(c,CHILD,[('task','item',digest)]); c.rollback()
    with pytest.raises(ReportNotReadyError, match='未确认'):
        StudyRepository(db).report_evidence(SID)
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='item'").fetchone()[0]=='completed'
        assert c.execute("SELECT artifact_type FROM capture_artifact WHERE artifact_id='candidate-test'").fetchone()[0]=='document_ocr_candidate'


@pytest.mark.parametrize('mutation', [
    "UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id='candidate-test'",
    "UPDATE capture_artifact SET session_id='other-session' WHERE artifact_id='candidate-test'",
    "UPDATE capture_artifact SET status='pending' WHERE artifact_id='candidate-test'",
    "DELETE FROM capture_artifact WHERE artifact_id='candidate-test'",
])
def test_changed_missing_or_foreign_completion_source_invalidates_report(db, mutation):
    with db.connect() as c:
        _artifact(c)
        insert(c,'daily_session',session_id='other-session',child_id=CHILD,status='completed',state='completed',
               started_at='2026-01-01',ended_at='2026-01-01',last_checkpoint_at='2026-01-01',client_action_id='other-session')
        c.execute("UPDATE plan_item SET completion_artifact_id='candidate-test' WHERE plan_item_id='item'")
        c.commit()
        before=report_source_fingerprint(c,SID)
        c.execute('BEGIN'); digest=_hash(c,CHILD,'task','item'); c.rollback()
        # Simulate a historic deletion if foreign-key enforcement is enabled.
        if mutation.startswith('DELETE'):
            c.execute('PRAGMA foreign_keys=OFF')
        c.execute(mutation); c.commit()
        assert report_source_fingerprint(c,SID) != before
        c.execute('BEGIN'); assert not valid(c,CHILD,[('task','item',digest)]); c.rollback()
    with pytest.raises(ReportNotReadyError, match='未确认'):
        StudyRepository(db).report_evidence(SID)


def test_task_time_projection_rejects_unconfirmed_historical_source(db):
    from home_study_agent.daily_study_duration import project_recorded_task_time
    with db.connect() as c:
        _artifact(c, 'document_ocr_candidate')
        c.execute("""UPDATE plan_item SET completion_artifact_id='candidate-test',started_at='2026-01-01T04:00:00+00:00',
            completed_at='2026-01-01T04:05:00+00:00',active_seconds=300 WHERE plan_item_id='item'""")
        c.commit(); c.execute('BEGIN')
        with pytest.raises(ValueError, match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            project_recorded_task_time(c, CHILD, '2026-01-01')
        c.rollback()


def test_cross_session_weekly_task_source_is_bound_to_report(db):
    from home_study_agent.report_calendar import study_day
    with db.connect() as c:
        day = study_day(c, SID)
        report_end = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (SID,)).fetchone()[0]
        assert report_end
        insert(c,'daily_session',session_id='previous-session',child_id=CHILD,status='completed',state='completed',
               started_at=report_end,ended_at=report_end,last_checkpoint_at=report_end,client_action_id='previous-session')
        insert(c,'daily_plan',plan_id='previous-plan',child_id=CHILD,session_id='previous-session',plan_date=day,
               status='completed',total_minutes=20,generator='test',version=1,created_at=report_end,client_action_id='previous-plan')
        insert(c,'capture_artifact',artifact_id='previous-art',child_id=CHILD,session_id='previous-session',
               artifact_type='document_ocr',status='completed',details_json='{}',created_at=report_end,client_action_id='previous-art')
        insert(c,'plan_item',plan_item_id='previous-task',plan_id='previous-plan',subject='数学',title='先前任务',
               expected_minutes=20,actual_minutes=15,priority=1,status='completed',position=0,source_type='pet_daily',
               completed_at=report_end,completion_artifact_id='previous-art')
        c.execute("UPDATE weekly_goal SET week_start=?,week_end=? WHERE weekly_goal_id='week'", (day,day))
        c.commit()
        from home_study_agent.report_task_provenance import aggregate_task_ids
        assert 'previous-task' in aggregate_task_ids(c, CHILD, day, report_end)
        before=report_source_fingerprint(c,SID)
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='previous-art'")
        c.commit()
        assert report_source_fingerprint(c,SID) != before
    with pytest.raises(ReportNotReadyError, match='未确认'):
        StudyRepository(db).report_evidence(SID)
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr' WHERE artifact_id='previous-art'")
        # This general fingerprint fixture intentionally seeds an incomplete
        # parent feedback stub; it is unrelated to the report aggregate.
        c.execute("DELETE FROM parent_action_feedback_event WHERE event_id='action-feedback'")
        c.execute("DELETE FROM goal_cycle WHERE cycle_id='cycle'")
        c.commit()
    result = StudyRepository(db).report_evidence(SID)
    assert '已记录15分钟' in result['goal_context'] or any(
        goal['completed_minutes'] == 15 for goal in result['goal_cycles'])


def test_unbound_homework_source_ref_is_not_treated_as_a_proven_task(db):
    with db.connect() as c:
        before=report_source_fingerprint(c,SID)
        c.execute("UPDATE plan_item SET source_type='school_homework',source_ref='unbound-hw' WHERE plan_item_id='item'")
        c.commit()
        assert report_source_fingerprint(c,SID) != before
        c.execute('BEGIN')
        with pytest.raises(ValueError, match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            _hash(c,CHILD,'task','item')
        c.rollback()
    with pytest.raises(ReportNotReadyError, match='未确认'):
        StudyRepository(db).report_evidence(SID)


def test_fabricated_cross_session_source_version_is_unconfirmed(db):
    import json
    with db.connect() as c:
        insert(c,'daily_session',session_id='origin',child_id=CHILD,status='completed',state='completed',
               started_at='2026-01-01',ended_at='2026-01-01',last_checkpoint_at='2026-01-01',client_action_id='origin')
        insert(c,'capture_artifact',artifact_id='origin-art',child_id=CHILD,session_id='origin',
               artifact_type='document_ocr',status='completed',details_json='{}',created_at='2026-01-01',client_action_id='origin-art')
        insert(c,'homework_item',homework_id='carried',child_id=CHILD,session_id=SID,subject='数学',task_type='written',
               title='合成延续作业',expected_minutes=20,source_artifact_id='origin-art',confirmation_status='child_confirmed',
               status='active',created_at='2026-01-01',client_action_id='carried')
        c.execute("UPDATE plan_item SET homework_id='carried',source_ref='carried' WHERE plan_item_id='item'")
        insert(c,'homework_item',homework_id='origin-hw',child_id=CHILD,session_id='origin',subject='数学',task_type='written',
               title='合成原作业',expected_minutes=20,source_artifact_id='origin-art',confirmation_status='child_confirmed',
               status='active',created_at='2026-01-01',client_action_id='origin-hw')
        c.execute('''CREATE TABLE learning_input_carryover_event(event_id TEXT,obligation_id TEXT,child_id TEXT,source_session_id TEXT,
            target_session_id TEXT,operation TEXT,result_json TEXT,revision INTEGER,client_action_id TEXT,request_json TEXT)''')
        c.execute('''CREATE TABLE learning_input_replan_unplaced(obligation_id TEXT,receipt_id TEXT,child_id TEXT,session_id TEXT,payload_json TEXT,reason TEXT)''')
        c.execute('''CREATE TABLE learning_input_replan_receipt(receipt_id TEXT,child_id TEXT,session_id TEXT,event_id TEXT,source_record_json TEXT)''')
        c.execute('''CREATE TABLE learning_input_revision_event(event_id TEXT,child_id TEXT,session_id TEXT,target_kind TEXT,target_id TEXT,after_json TEXT)''')
        from home_study_agent.learning_input_revision import _version
        original=dict(c.execute("SELECT * FROM homework_item WHERE homework_id='origin-hw'").fetchone())
        artifact=dict(c.execute("SELECT * FROM capture_artifact WHERE artifact_id='origin-art'").fetchone())
        binding={'version':'learning_input_replan_source_v1','event_id':'revision','child_id':CHILD,
                 'session_id':'origin','homework_id':'origin-hw','source':original,'artifact_fingerprint':_version(artifact)}
        c.execute('INSERT INTO learning_input_revision_event VALUES(?,?,?,?,?,?)',('revision',CHILD,'origin','homework','origin-hw',json.dumps(original)))
        c.execute('INSERT INTO learning_input_replan_receipt VALUES(?,?,?,?,?)',('receipt',CHILD,'origin','revision',json.dumps(original)))
        c.execute('INSERT INTO learning_input_replan_unplaced VALUES(?,?,?,?,?,?)',
                  ('obligation','receipt',CHILD,'origin',json.dumps({'source_binding':binding}),'unplaced'))
        request={'child_id':CHILD,'obligation_id':'obligation','target_session_id':SID,'decision':'continue_today',
                 'expected_revision':0,'expected_source_version':'v','expected_context_version':'ctx',
                 'client_action_id':'carry-action','authorized_turn_id':'carry-action','request_quote':'今天继续','not_before':None}
        from home_study_agent.learning_input_carryover import LearningInputCarryoverService
        command_binding=LearningInputCarryoverService._binding(request)
        arguments={'decision':'continue_today','request_quote':'今天继续','not_before':None}
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES('carry-action',?,?,'今天继续','input_carryover','','{}','completed','test','2026-01-01','carry-action')""",(CHILD,SID))
        c.execute("""INSERT INTO agent_plan(plan_id,child_id,session_id,agent_turn_id,planner_version,context_manifest_json,plan_json,status,created_at,updated_at)
            VALUES('carry-action',?,?,'carry-action','test',?,'{}','completed','2026-01-01','2026-01-01')""",(CHILD,SID,json.dumps({'learning_input_unplaced_binding':command_binding})))
        c.execute("""INSERT INTO agent_step(step_id,plan_id,step_index,capability_name,capability_version,execution_location,risk_level,arguments_json,expected_evidence_json,status,idempotency_key,created_at,updated_at)
            VALUES('carry-action','carry-action',0,'learning_inputs.review_unplaced','1','server','medium',?,'[]','completed','carry-action','2026-01-01','2026-01-01')""",(json.dumps(arguments),))
        receipt={'event_id':'event','obligation_id':'obligation','revision':1,'target_homework_id':'carried',
                 'target_homework':{'homework_id':'carried','child_id':CHILD,'session_id':SID,'source_artifact_id':'origin-art'}}
        c.execute('INSERT INTO learning_input_carryover_event VALUES(?,?,?,?,?,?,?,?,?,?)',
                  ('event','obligation',CHILD,'origin',SID,'continue_today',json.dumps(receipt),1,'carry-action',json.dumps(request)))
        c.commit()
        # This hand-built fixture never came through the authoritative replan
        # service: its arbitrary source version must not qualify as evidence.
        # Real accepted multi-session paths are exercised in
        # test_report_candidate_carryover.py.
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            _hash(c,CHILD,'task','item')
        c.rollback()
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='origin-art'"); c.commit()
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            _hash(c,CHILD,'task','item')
        c.rollback()
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr' WHERE artifact_id='origin-art'")
        c.execute("DELETE FROM learning_input_carryover_event WHERE event_id='event'"); c.commit()
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            _hash(c,CHILD,'task','item')
        c.rollback()
        assert c.execute("SELECT source_artifact_id FROM homework_item WHERE homework_id='carried'").fetchone()[0]=='origin-art'


def test_cross_session_goal_cycle_source_is_bound(db):
    from home_study_agent.report_calendar import study_day
    from home_study_agent.report_task_provenance import aggregate_task_ids
    with db.connect() as c:
        day=study_day(c,SID); end=c.execute('SELECT ended_at FROM daily_session WHERE session_id=?',(SID,)).fetchone()[0]
        program=c.execute("SELECT program_id FROM goal_program WHERE child_id=? AND track='competition_math_path'",(CHILD,)).fetchone()[0]
        c.execute("UPDATE goal_cycle SET week_start=?,week_end=?,program_id=? WHERE cycle_id='cycle'",(day,day,program))
        insert(c,'daily_session',session_id='prior-cycle',child_id=CHILD,status='completed',state='completed',
               started_at=end,ended_at=end,last_checkpoint_at=end,client_action_id='prior-cycle')
        insert(c,'daily_plan',plan_id='prior-cycle-plan',child_id=CHILD,session_id='prior-cycle',plan_date=day,
               status='completed',total_minutes=10,generator='test',version=1,created_at=end,client_action_id='prior-cycle-plan')
        insert(c,'capture_artifact',artifact_id='prior-cycle-art',child_id=CHILD,session_id='prior-cycle',
               artifact_type='document_ocr',status='completed',details_json='{}',created_at=end,client_action_id='prior-cycle-art')
        insert(c,'plan_item',plan_item_id='prior-cycle-item',plan_id='prior-cycle-plan',subject='数学',title='合成目标',
               expected_minutes=10,actual_minutes=10,priority=1,status='completed',position=0,source_type='competition_math',
               completed_at=end,completion_artifact_id='prior-cycle-art')
        c.commit()
        assert 'prior-cycle-item' in aggregate_task_ids(c,CHILD,day,end)
        before=report_source_fingerprint(c,SID)
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='prior-cycle-art'");c.commit()
        assert report_source_fingerprint(c,SID)!=before
    with pytest.raises(ReportNotReadyError,match='未确认'):
        StudyRepository(db).report_evidence(SID)

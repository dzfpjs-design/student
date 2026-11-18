"""Exercise provenance guard against real child-confirmed carryover records."""
from datetime import datetime, timezone

import pytest

from home_study_agent.learning_input_carryover import LearningInputCarryoverService
from home_study_agent.report_task_provenance import task_source_state
from test_learning_input_carryover import schedule, revised, plan, grant, CHILD, inputs, flow


def _accepted_carryover(schedule):
    source, replan, clock = schedule
    db, original_sid, _ = source
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=10 WHERE child_id=?', (CHILD,))
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,details_json,status,created_at,client_action_id)
            VALUES ('synthetic-ocr',?,?,'document_ocr','{}','completed',datetime('now'),'synthetic-ocr')""", (CHILD, original_sid))
        c.execute("UPDATE homework_item SET source_artifact_id='synthetic-ocr' WHERE homework_id='math-a'")
        c.commit()
    event = revised(schedule)
    initial = replan.execute_replan(CHILD,event,'unplaced-source')
    clock[0] = datetime(2026,9,8,10,tzinfo=timezone.utc)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id=?", (clock[0].isoformat(), original_sid))
        c.execute('UPDATE family_rule SET max_learning_minutes=120 WHERE child_id=?', (CHILD,))
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('today',?,'active','homework_capture',?,?,'today')""", (CHILD,clock[0].isoformat(),clock[0].isoformat()))
        c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES ('today','medium',0,0,?)", (clock[0].isoformat(),))
        c.commit()
    service=LearningInputCarryoverService(db); service.initialize()
    carry=(db,service,initial['unplaced'][0]['obligation_id'],clock)
    plan(db)
    result=service.review_unplaced(**grant(carry))
    item_id=next(item['plan_item_id'] for item in result['newitems'] if item['source_type']=='school_homework')
    return db,service,carry,clock,original_sid,item_id


def test_cloud_report_authority_accepts_valid_carried_task_through_final_recheck(schedule):
    from home_study_agent.cloud_parent_reports import ParentReportGenerationAuthority, ParentReportAuthorityChangedError
    from home_study_agent.repository import StudyRepository
    from home_study_agent.reports import build_daily_report
    db,_,_,clock,_,_=_accepted_carryover(schedule)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='today'",(clock[0].isoformat(),))
        c.execute("INSERT OR REPLACE INTO privacy_preference VALUES(1,1,0,0,0,30,30,30,?)",(clock[0].isoformat(),))
        c.execute("""INSERT INTO daily_reflection VALUES('synthetic-reflection','today',?,'','','okay','',1,?,'synthetic-reflection')""",(CHILD,clock[0].isoformat()))
        c.execute("INSERT INTO consent_record VALUES('synthetic-cloud-consent','cloud_model_text_allowed',1,'privacy_v1','parent_ui',?)",(clock[0].isoformat(),))
        c.commit()
    repo=StudyRepository(db)
    evidence=repo.report_evidence('today');summary,baseline=build_daily_report(evidence)
    version=ParentReportGenerationAuthority.capture_source(repo,'today')
    authority=ParentReportGenerationAuthority(repo,evidence,summary,baseline,lambda:repo.report_evidence('today'),version)
    authority()
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='synthetic-ocr'")
        c.commit()
    with pytest.raises(ParentReportAuthorityChangedError):
        authority()


def test_report_accepts_real_carried_ocr_source_without_copying_session(schedule, monkeypatch):
    db,service,carry,clock,original_sid,item_id=_accepted_carryover(schedule)
    with db.connect() as c:
        assert not c.in_transaction
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        assert not c.in_transaction
        c.execute('BEGIN')
        state=task_source_state(c,CHILD,item_id)
        assert {entry[0] for entry in state} == {'carryover','homework','homework_identity','homework_record'}
        assert any(entry[0]=='homework' and entry[3]==original_sid for entry in state)
    # A second, real reschedule/reclaim in the next workflow step must still
    # trace both accepted receipts back to the first camera source.
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    repository.StudyRepository(db).reschedule_plan_item(item_id,'稍后继续','synthetic-defer')
    with db.connect() as c:
        row = c.execute("SELECT obligation_id FROM learning_input_replan_unplaced WHERE json_extract(payload_json,'$.reclaim.plan_item_id')=?",(item_id,)).fetchone()
        assert row is not None, c.execute("SELECT payload_json FROM plan_item_event WHERE client_action_id='synthetic-defer'").fetchone()[0]
        reclaimed_id = row[0]
    second=service.review_unplaced(**grant(carry,obligation_id=reclaimed_id))
    item_id=next(item['plan_item_id'] for item in second['newitems'] if item['source_type']=='school_homework')
    with db.connect() as c:
        c.execute('BEGIN')
        lineage=task_source_state(c,CHILD,item_id)
        assert sum(entry[0]=='carryover' for entry in lineage) == 2
        assert sum(entry[0]=='homework_identity' for entry in lineage) == 3
        from home_study_agent.daily_report_frozen_dependencies import _hash, valid
        from home_study_agent.report_source_fingerprint import report_source_fingerprint
        digest=_hash(c,CHILD,'task',item_id); source_before=report_source_fingerprint(c,'today'); c.rollback()
        top_obligation=next(entry[5] for entry in lineage if entry[0]=='carryover')
        c.execute('BEGIN')
        c.execute("""INSERT INTO learning_input_carryover_event(event_id,obligation_id,child_id,source_session_id,target_session_id,revision,operation,client_action_id,request_json,result_json,created_at)
            SELECT 'synthetic-withdraw',obligation_id,child_id,source_session_id,target_session_id,999,'withdraw_scope','synthetic-withdraw',request_json,result_json,created_at
            FROM learning_input_carryover_event WHERE obligation_id=? ORDER BY revision DESC LIMIT 1""",(top_obligation,))
        assert service._origin(c,CHILD,top_obligation)['source_current'] is False
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        assert not valid(c,CHILD,[('task',item_id,digest)])
        assert report_source_fingerprint(c,'today') != source_before
        c.rollback()
        # If the retained agent authorization no longer binds the child's
        # request, the receipt cannot qualify as current report evidence.
        import json
        top_event=next(entry[1] for entry in lineage if entry[0]=='carryover')
        request=json.loads(c.execute('SELECT request_json FROM learning_input_carryover_event WHERE event_id=?',(top_event,)).fetchone()[0])
        c.execute('BEGIN')
        c.execute("UPDATE agent_turn SET transcript='不同的合成指令' WHERE agent_turn_id=?",(request['authorized_turn_id'],))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        assert not valid(c,CHILD,[('task',item_id,digest)])
        assert report_source_fingerprint(c,'today') != source_before
        c.rollback()
        # A root edit leaves the historic carryover receipt intact but removes
        # current report qualification until an explicit correction is made.
        c.execute('BEGIN')
        c.execute("UPDATE homework_item SET title='修订后的合成源作业' WHERE homework_id='math-a'")
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        assert not valid(c,CHILD,[('task',item_id,digest)])
        assert report_source_fingerprint(c,'today') != source_before
        c.rollback()
        top=next(entry for entry in lineage if entry[0]=='carryover')
        c.execute('BEGIN')
        c.execute("""INSERT INTO learning_input_revision_event(event_id,child_id,session_id,target_kind,target_id,operation,before_json,after_json,request_id,confirmation_arrival_id,undo_event_id,impact_json,created_at,client_action_id)
            SELECT 'newer-revision',child_id,session_id,target_kind,target_id,operation,before_json,after_json,request_id,confirmation_arrival_id,event_id,impact_json,created_at,'newer-revision'
            FROM learning_input_revision_event WHERE event_id=?""",(top[6],))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        assert not valid(c,CHILD,[('task',item_id,digest)])
        c.rollback()
        c.execute('BEGIN')
        c.execute("UPDATE capture_artifact SET extracted_text='订正后的合成 OCR' WHERE artifact_id='synthetic-ocr'")
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        c.rollback()
        c.execute('BEGIN')
        c.execute("UPDATE learning_input_carryover_event SET result_json='{}' WHERE event_id=?",(top[1],))
        assert not valid(c,CHILD,[('task',item_id,digest)])
        assert report_source_fingerprint(c,'today') != source_before
        c.rollback()
        # A cyclic predecessor must fail without recursion or rewriting records.
        import json
        c.execute('BEGIN')
        raw=c.execute('SELECT payload_json FROM learning_input_replan_unplaced WHERE obligation_id=?',(top[5],)).fetchone()[0]
        payload=json.loads(raw)
        current=c.execute('SELECT h.homework_id,h.session_id FROM plan_item i JOIN homework_item h ON h.homework_id=i.homework_id WHERE i.plan_item_id=?',(item_id,)).fetchone()
        payload['reclaim']['source_homework']['homework_id']=current[0]
        payload['reclaim']['source_homework']['session_id']=current[1]
        payload['reclaim']['source_session_id']=current[1]
        c.execute('UPDATE learning_input_replan_unplaced SET payload_json=? WHERE obligation_id=?',(json.dumps(payload),top[5]))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)
        c.rollback()

    # A same-child image from an unrelated session must not inherit the
    # legitimacy of a different homework's carryover receipt.
    with db.connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('unrelated',?,'completed','completed',?,?,'unrelated')""", (CHILD,clock[0].isoformat(),clock[0].isoformat()))
        c.execute("UPDATE capture_artifact SET session_id='unrelated' WHERE artifact_id='synthetic-ocr'")
        c.commit()
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,item_id)


def test_authorized_confirmed_scope_remains_valid_but_its_withdrawal_invalidates(schedule,monkeypatch):
    from datetime import timedelta
    from home_study_agent import repository
    from test_learning_input_reclaim import scope_command
    db,service,carry,clock,_,item_id=_accepted_carryover(schedule)
    monkeypatch.setattr(repository,'utc_now',lambda:clock[0].isoformat())
    repo=repository.StudyRepository(db)
    repo.start_plan_item(item_id,'scope-start')
    clock[0]+=timedelta(minutes=4)
    repo.pause_plan_item(item_id,'稍后','scope-pause')
    repo.reschedule_plan_item(item_id,'剩余范围待确认','scope-defer')
    with db.connect() as c:
        oid=c.execute("SELECT obligation_id FROM learning_input_replan_unplaced WHERE json_extract(payload_json,'$.reclaim.plan_item_id')=?",(item_id,)).fetchone()[0]
    confirmed=service.review_reclaimed_scope(**scope_command(carry,oid,minutes=5))
    assert confirmed['status']=='scope_confirmed'
    result=service.review_unplaced(**grant(carry,obligation_id=oid))
    next_item=next(item['plan_item_id'] for item in result['newitems'] if item['source_type']=='school_homework')
    with db.connect() as c:
        c.execute('BEGIN')
        valid_state=task_source_state(c,CHILD,next_item)
        assert sum(item[0]=='carryover' for item in valid_state)==2
        original_factory=c.row_factory
        try:
            c.row_factory=None
            assert task_source_state(c,CHILD,next_item)==valid_state
            assert c.row_factory is None
        finally:
            c.row_factory=original_factory
        import json
        scope_request=json.loads(c.execute('SELECT request_json FROM learning_input_carryover_event WHERE event_id=?',(confirmed['event_id'],)).fetchone()[0])
        c.rollback()
        c.execute('BEGIN')
        c.execute('DELETE FROM learning_input_carryover_event WHERE event_id=?',(confirmed['event_id'],))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,next_item)
        c.rollback()
        c.execute('BEGIN')
        reclaim_payload=json.loads(c.execute('SELECT payload_json FROM learning_input_replan_unplaced WHERE obligation_id=?',(oid,)).fetchone()[0])
        reclaim_payload['reclaim']['requires_scope_review']=False
        c.execute('UPDATE learning_input_replan_unplaced SET payload_json=? WHERE obligation_id=?',(json.dumps(reclaim_payload),oid))
        c.execute('DELETE FROM learning_input_carryover_event WHERE event_id=?',(confirmed['event_id'],))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,next_item)
        c.rollback()
        c.execute('BEGIN')
        c.execute("UPDATE agent_turn SET transcript='不同的合成确认' WHERE agent_turn_id=?",(scope_request['authorized_turn_id'],))
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,next_item)
        c.rollback()
    service.review_reclaimed_scope(**scope_command(carry,oid,'withdraw_scope'))
    with db.connect() as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='REPORT_TASK_SOURCE_UNCONFIRMED'):
            task_source_state(c,CHILD,next_item)
        assert c.execute('SELECT COUNT(*) FROM plan_item WHERE plan_item_id=?',(next_item,)).fetchone()[0]==1

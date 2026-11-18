"""Synthetic adversarial cases; no home data. Expected to fail before fixes."""
import json
import sqlite3
import pytest
from home_study_agent.report_task_provenance import task_source_state
from home_study_agent.repository import StudyRepository, ReportNotReadyError
from home_study_agent.reports import build_daily_report
from test_report_source_fingerprint import db, SID, CHILD, insert


def clean_fixture_report(d):
    with d.connect() as c:
        c.execute('PRAGMA foreign_keys=OFF')
        for table in ('parent_action_feedback_event','parent_action_snapshot','report_feedback_revision',
                      'parent_report_snapshot_feedback','parent_report_snapshot','daily_report'):
            c.execute('DELETE FROM ' + table)
        c.commit()


def seed_artifact(d):
    with d.connect() as c:
        insert(c, 'capture_artifact', artifact_id='synthetic-x', child_id=CHILD, session_id=SID,
               artifact_type='document_ocr', status='completed', details_json='{}',
               created_at='2026-01-01', client_action_id='synthetic-x')
        c.execute("UPDATE plan_item SET completion_artifact_id='synthetic-x' WHERE plan_item_id='item'")
        c.commit()


def test_carryover_rejects_unrelated_earlier_artifact():
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE daily_plan(plan_id,child_id,session_id);
      CREATE TABLE plan_item(plan_item_id,plan_id,homework_id,completion_artifact_id,source_type,source_ref);
      CREATE TABLE homework_item(homework_id,child_id,session_id,source_artifact_id);
      CREATE TABLE capture_artifact(artifact_id,child_id,session_id,artifact_type,status);
      CREATE TABLE learning_input_carryover_event(event_id,source_session_id,target_session_id,result_json,child_id,operation);''')
    c.execute('INSERT INTO daily_plan VALUES (?,?,?)',('p','child','target'))
    c.execute('INSERT INTO plan_item VALUES (?,?,?,?,?,?)',('i','p','h',None,'school_homework','h'))
    c.execute('INSERT INTO homework_item VALUES (?,?,?,?)',('h','child','target','artifact'))
    c.execute('INSERT INTO capture_artifact VALUES (?,?,?,?,?)',('artifact','child','unrelated','document_ocr','completed'))
    receipt={'target_homework_id':'h','target_homework':{'homework_id':'h','child_id':'child',
             'session_id':'target','source_artifact_id':'artifact'}}
    c.execute('INSERT INTO learning_input_carryover_event VALUES (?,?,?,?,?,?)',
              ('event','different-source','target',json.dumps(receipt),'child','continue_today'))
    with pytest.raises(ValueError): task_source_state(c,'child','i')


def test_existing_report_rechecks_candidate(db):
    clean_fixture_report(db); seed_artifact(db); repo=StudyRepository(db)
    summary, coaching = build_daily_report(repo.report_evidence(SID))
    report=repo.save_daily_report(SID, summary, coaching)
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='synthetic-x'"); c.commit()
    with pytest.raises(ReportNotReadyError): repo.get_daily_report(report['report_id'])


def test_save_rechecks_candidate_in_commit_transaction(db):
    clean_fixture_report(db); seed_artifact(db); repo=StudyRepository(db)
    summary, coaching = build_daily_report(repo.report_evidence(SID))
    def mutate(c):
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='synthetic-x'")
    with pytest.raises(ReportNotReadyError): repo.save_daily_report(SID, summary, coaching, pre_persist=mutate)


def test_foreign_child_plan_not_projected_as_current_session(db):
    clean_fixture_report(db)
    with db.connect() as c:
        insert(c,'capture_artifact',artifact_id='synthetic-x',child_id='child-junior-3',session_id=SID,
               artifact_type='document_ocr_candidate',status='completed',details_json='{}',
               created_at='2026-01-01',client_action_id='synthetic-x')
        c.execute("UPDATE daily_plan SET child_id='child-junior-3' WHERE plan_id='plan'")
        c.execute("UPDATE plan_item SET completion_artifact_id='synthetic-x' WHERE plan_item_id='item'")
        c.commit()
    with pytest.raises(ReportNotReadyError): StudyRepository(db).report_evidence(SID)

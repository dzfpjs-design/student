"""Existing task rows survive a supported synthetic schema upgrade."""
import sqlite3
from historical_database_fixture import seed_empty_historical_v84
from home_study_agent.database import Database
from home_study_agent.report_task_provenance import task_source_state
from home_study_agent.daily_report_frozen_dependencies import _hash


def test_v84_historical_task_without_ocr_reference_remains_reportable(tmp_path):
    path=tmp_path/'historical-v84.db'
    seed_empty_historical_v84(path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('a','样例','四年级','南京','active','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,ended_at,last_checkpoint_at,client_action_id) VALUES('s','a','completed','completed','2026-01-01','2026-01-01','2026-01-01','s')")
        c.execute("INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id) VALUES('p','a','s','2026-01-01','completed',10,'test',1,'2026-01-01','p')")
        c.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position) VALUES('i','p','数学','原任务',10,1,'completed',0)")
    db=Database(path); db.initialize()
    with db.connect() as c:
        c.execute('BEGIN')
        assert task_source_state(c,'a','i') == []
        assert len(_hash(c,'a','task','i')) == 64
        assert tuple(c.execute("SELECT title,status FROM plan_item WHERE plan_item_id='i'").fetchone()) == ('原任务','completed')
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        c.rollback()

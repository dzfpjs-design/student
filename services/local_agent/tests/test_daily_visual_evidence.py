import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from home_study_agent.daily_visual_evidence import project_daily_visual_evidence


class Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'synthetic.db')
    with database.connect() as c:
        c.executescript('''
        CREATE TABLE deletion_job(child_id TEXT, scope TEXT, target_type TEXT, status TEXT);
        CREATE TABLE daily_session(session_id TEXT, child_id TEXT);
        CREATE TABLE capture_artifact(artifact_id TEXT, child_id TEXT, session_id TEXT, extracted_text TEXT, status TEXT, artifact_type TEXT, details_json TEXT DEFAULT '{}');
        CREATE TABLE document_page_revision(revision_id TEXT, child_id TEXT, session_id TEXT, artifact_id TEXT,
          content_sha256 TEXT, ocr_confidence REAL, review_required INTEGER, created_at TEXT);
        CREATE TABLE observed_question(question_id TEXT,child_id TEXT,canonical_text TEXT,question_fingerprint TEXT);
        CREATE TABLE observed_question_occurrence(occurrence_id TEXT,revision_id TEXT,artifact_id TEXT,child_id TEXT,
          observed_at TEXT,confidence REAL,question_id TEXT);
        ''')
        c.executemany('INSERT INTO daily_session VALUES (?,?)', [('s1','child1'),('s2','child2')])
        c.commit()
    return database


def add_page(db, ident, child, session, stamp, text='1. 求 2+2', confidence=.9):
    with db.connect() as c:
        c.execute('INSERT INTO capture_artifact(artifact_id,child_id,session_id,extracted_text,status,artifact_type) VALUES (?,?,?,?,?,?)', ('a'+ident,child,session,text,'completed','document_ocr'))
        c.execute('INSERT INTO document_page_revision VALUES (?,?,?,?,?,?,?,?)',
                  ('r'+ident,child,session,'a'+ident,hashlib.sha256(text.encode()).hexdigest(),confidence,0,stamp))
        compact = ''.join(text.split()).casefold()
        fingerprint = hashlib.sha256(compact.encode()).hexdigest()
        c.execute('INSERT INTO observed_question VALUES (?,?,?,?)', ('q'+ident,child,text,fingerprint))
        c.execute('INSERT INTO observed_question_occurrence VALUES (?,?,?,?,?,?,?)',
                  ('o'+ident,'r'+ident,'a'+ident,child,stamp,confidence,'q'+ident))
        c.commit()


def test_cloud_candidate_is_visible_and_marked_unconfirmed_when_local_ocr_empty(db):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00',text='',confidence=.1)
    with db.connect() as c:
        details={'cloud_vision_fallback':{'status':'needs_review','privacy':{'original_frame_uploaded':False},'blocks':[{'text':'地理：世界的气候'}]}}
        c.execute('UPDATE capture_artifact SET details_json=?',(json.dumps(details,ensure_ascii=False),))
        c.execute('DELETE FROM observed_question_occurrence')
        c.commit()
    result=project_daily_visual_evidence(db,'child1','2026-11-04')
    assert result['pages'][0]['text']=='地理：世界的气候'
    assert result['pages'][0]['source']=='cloud_page'
    assert result['pages'][0]['observations'][0]['review_required'] is True
    assert not result['excluded_invalid_count']


@pytest.mark.parametrize('cloud',[
    {'status':'blocked','privacy':{'original_frame_uploaded':False},'blocks':[{'text':'do not use'}]},
    {'status':'needs_review','privacy':{'original_frame_uploaded':True},'blocks':[{'text':'do not use'}]},
    {'status':'needs_review','blocks':[{'text':'do not use'}]},
])
def test_unverifiable_cloud_candidate_is_not_presented(db,cloud):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00',text='',confidence=.1)
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=?',(json.dumps({'cloud_vision_fallback':cloud}),))
        c.commit()
    assert not project_daily_visual_evidence(db,'child1','2026-11-04')['pages']


def test_china_day_offsets_dedup_child_and_low_confidence(db):
    add_page(db,'a','child1','s1','2026-11-04T00:00:00+08:00',confidence=.4)
    add_page(db,'b','child1','s1','2026-11-04T15:59:59Z')
    add_page(db,'c','child1','s1','2026-11-04T16:00:00+00:00',text='second day')
    add_page(db,'d','child2','s2','2026-11-04T01:00:00+08:00',text='other child')
    add_page(db,'e','child1','s1','2026-11-04T12:00:00',text='timezone missing')
    result = project_daily_visual_evidence(db,'child1','2026-11-04')
    assert len(result['pages']) == len(result['questions']) == 1
    assert len(result['pages'][0]['observations']) == 2
    assert result['pages'][0]['observations'][0]['review_required'] is True
    assert result['questions'][0]['observations'][0]['review_required'] is True
    assert 'other child' not in str(result) and 'second day' not in str(result)
    assert 'timezone missing' not in str(result)
    assert result['interpretation'] == 'ocr_observation_not_learning_or_mastery'


def test_source_deleted_or_rewritten_disappears_and_changes_fingerprint(db):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00')
    before = project_daily_visual_evidence(db,'child1','2026-11-04')
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET extracted_text=?',('changed after capture',))
        c.commit()
    after = project_daily_visual_evidence(db,'child1','2026-11-04')
    assert not after['pages'] and not after['questions']
    assert after['source_fingerprint'] != before['source_fingerprint']
    with db.connect() as c:
        c.execute('DELETE FROM capture_artifact')
        c.commit()
    assert not project_daily_visual_evidence(db,'child1','2026-11-04')['pages']


def test_pending_child_or_capture_deletion_fails_closed(db):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00')
    with db.connect() as c:
        c.execute('INSERT INTO deletion_job VALUES (?,?,?,?)',('child1','capture_artifact','capture_artifact','deleting'))
        c.commit()
    with pytest.raises(ValueError,match='DELETION_PENDING'):
        project_daily_visual_evidence(db,'child1','2026-11-04')
    assert not project_daily_visual_evidence(db,'child2','2026-11-04')['pages']


def test_high_question_confidence_cannot_override_low_page_confidence(db):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00',confidence=.4)
    with db.connect() as c:
        c.execute('UPDATE observed_question_occurrence SET confidence=?', (.99,))
        c.commit()
    result = project_daily_visual_evidence(db,'child1','2026-11-04')
    assert result['questions'][0]['observations'][0]['confidence'] == .99
    assert result['questions'][0]['observations'][0]['review_required'] is True


def test_non_document_artifact_is_not_a_camera_page(db):
    add_page(db,'a','child1','s1','2026-11-04T01:00:00+08:00')
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET artifact_type='other'")
        c.commit()
    result = project_daily_visual_evidence(db,'child1','2026-11-04')
    assert not result['pages'] and not result['questions']


def test_real_document_monitor_round_trip_and_deletion(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from home_study_agent.database import Database as RealDatabase
    from home_study_agent.document_monitoring import DocumentMonitoringService
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.repository import utc_now

    database = RealDatabase(tmp_path / 'real.db')
    database.initialize()
    now = utc_now()
    with database.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','学生','初三','江苏','active',?,?)", (now,now))
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('session','child','active','study',?,?,?)", (now,now,'session-action'))
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,
            confidence,details_json,status,created_at,client_action_id) VALUES('artifact','child','session','document_ocr',NULL,?,.9,'{}','completed',?,'capture-action')""", ('1. 求 2+2', now))
        c.commit()
    service = DocumentMonitoringService(database)
    service.start('session','monitor-start')
    service.record_revision('session','artifact','synthetic-page','revision-action')
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    projected = project_daily_visual_evidence(database,'child',day)
    assert projected['pages'][0]['text'] == '1. 求 2+2'
    assert projected['pages'][0]['observations'][0]['artifact_id'] == 'artifact'
    local_summary = LearningStateService(database).daily_learning_summary('child', day)
    assert local_summary['document_observation']['pages'][0]['text'] == '1. 求 2+2'
    assert '1. 求 2+2' in local_summary['child_response']
    assert '不代表已经掌握' in local_summary['child_response']
    assert local_summary['cloud_transmission_allowed'] is False
    with database.connect() as c:
        c.execute("""INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at)
                     VALUES('deleting','child','capture_artifact','capture_artifact','artifact','deleting',?,?)""", (now,now))
        c.commit()
    pending = LearningStateService(database).daily_learning_summary('child', day)
    assert pending['document_observation'] == {'status': 'unavailable', 'reason': 'DELETION_PENDING'}
    assert '1. 求 2+2' not in pending['child_response']
    with database.connect() as c:
        c.execute("DELETE FROM capture_artifact WHERE artifact_id='artifact'")
        c.execute("UPDATE deletion_job SET status='completed' WHERE deletion_id='deleting'")
        c.commit()
    assert not project_daily_visual_evidence(database,'child',day)['pages']
    assert '1. 求 2+2' not in LearningStateService(database).daily_learning_summary('child', day)['child_response']

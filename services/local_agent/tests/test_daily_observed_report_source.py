from datetime import datetime, timezone, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest

from home_study_agent.daily_observed_report_source import build_daily_observed_source
from home_study_agent.repository import utc_now
from test_document_monitoring import _service, _artifact


def _seed(tmp_path):
    db, monitor = _service(tmp_path)
    monitor.start('study', 'daily-observed-start')
    _artifact(db, 'page', '1. Synthetic question')
    revision = monitor.record_revision('study', 'page', 'page-signature', 'daily-observed-revision')
    now = datetime.now(timezone.utc)
    # Use a time fully inside the current local day, including around midnight.
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    start = local.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = start + timedelta(seconds=5)
    with db.connect() as c:
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('d','document_camera','synthetic','{}','active')")
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('l','study','d','document_camera','active',?,?,?)", (utc_now(),utc_now(),utc_now()))
        mid = c.execute("SELECT monitor_id FROM document_monitor_session WHERE session_id='study'").fetchone()[0]
        c.execute('INSERT INTO document_observation_segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('seg','study','child',mid,revision['revision_id'],'l','e',1,0,start.isoformat(),end.isoformat(),4500,'h',utc_now()))
        fp = sha256('1.syntheticquestion'.encode()).hexdigest()
        c.execute('''INSERT INTO observed_question(question_id,child_id,question_fingerprint,canonical_text,
            first_revision_id,latest_revision_id,latest_artifact_id,first_seen_at,last_seen_at)
            VALUES('q','child',?,'1. Synthetic question',?,?,'page',?,?)''', (fp,revision['revision_id'],revision['revision_id'],start.isoformat(),start.isoformat()))
        c.execute('''INSERT INTO observed_question_occurrence(occurrence_id,question_id,child_id,revision_id,artifact_id,confidence,observed_at)
            VALUES('occ','q','child',?,'page',0.8,?)''', (revision['revision_id'],start.isoformat()))
        c.execute("INSERT INTO child_state_event VALUES('ev','child','study','writing_like',0.5,'synthetic',0,'synthetic',1,?)", (start.isoformat(),))
        c.commit()
    return db, revision['revision_id'], local.date().isoformat()


def test_source_is_transactional_stable_and_no_reflection_required(tmp_path):
    db, revision, day = _seed(tmp_path)
    with db.connect() as c:
        with pytest.raises(ValueError, match='TRANSACTION_REQUIRED'):
            build_daily_observed_source(c,'child',day)
        c.execute('BEGIN')
        first = build_daily_observed_source(c,'child',day)
        assert c.in_transaction
        assert first == build_daily_observed_source(c,'child',day)
        assert first['pages'][0]['revision_id'] == revision
        assert first['pages'][0]['estimated_visible_ms'] == 4500
        assert first['pages'][0]['segments'][0]['segment_id'] == 'seg'
        assert first['questions'] and first['questions'][0]['revision_id'] == revision
        assert first['actual_learning_seconds'] is None and first['activity_inference'] is None
        assert first['camera_samples'][0]['state'] == 'writing_like' and first['camera_samples'][0]['shadow'] is True
        assert first['delivery_authorized'] is False and first['coverage_complete'] is False
        assert c.execute('SELECT count(*) FROM daily_reflection').fetchone()[0] == 0
        c.rollback()
    from home_study_agent.database import Database
    with Database(db.path).connect() as c:
        c.execute('BEGIN'); assert build_daily_observed_source(c,'child',day) == first


def test_child_day_and_deletion_isolation_and_source_change(tmp_path):
    db, _, day = _seed(tmp_path)
    with db.connect() as c:
        c.execute('BEGIN'); before = build_daily_observed_source(c,'child',day); c.rollback()
        c.execute("UPDATE capture_artifact SET extracted_text='tampered' WHERE artifact_id='page'"); c.commit()
        c.execute('BEGIN'); changed = build_daily_observed_source(c,'child',day); c.rollback()
        assert changed['pages'] == [] and changed['source_fingerprint'] != before['source_fingerprint']
        now = utc_now()
        c.execute("INSERT INTO child_profile VALUES('other','synthetic','初三','江苏','active',?,?)", (now,now)); c.commit()
        c.execute('BEGIN'); other = build_daily_observed_source(c,'other',day); c.rollback()
        assert other['pages'] == []
        tomorrow = (datetime.fromisoformat(day).date()+timedelta(days=1)).isoformat()
        c.execute('BEGIN'); assert build_daily_observed_source(c,'child',tomorrow)['pages'] == []; c.rollback()
        c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,status,created_at,updated_at) VALUES('deleting','child','media','child_scope','deleting',?,?)",(now,now)); c.commit()
        c.execute('BEGIN')
        with pytest.raises(ValueError, match='DELETION_PENDING'): build_daily_observed_source(c,'child',day)


def test_existing_read_transaction_keeps_consistent_source_snapshot(tmp_path):
    db, _, day = _seed(tmp_path)
    with db.connect() as reader:
        reader.execute('BEGIN')
        original = build_daily_observed_source(reader, 'child', day)
        with db.connect() as writer:
            writer.execute("UPDATE capture_artifact SET extracted_text='different' WHERE artifact_id='page'")
            writer.commit()
        assert build_daily_observed_source(reader, 'child', day) == original
        reader.rollback(); reader.execute('BEGIN')
        assert build_daily_observed_source(reader, 'child', day)['source_fingerprint'] != original['source_fingerprint']

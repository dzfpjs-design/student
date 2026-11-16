from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_daily_report_candidate import synthetic
from home_study_agent.daily_report_observation import _projection
from home_study_agent.daily_report_frozen_dependencies import build, valid
from home_study_agent.repository import utc_now


def projection(c, child, day):
    return _projection(c,child,day)


def test_freeze_all_events_and_allow_new_events_and_session_end(synthetic):
    db,day=synthetic
    now=utc_now()
    with db.connect() as c:
        for i in range(101):
            c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES(?,'a','s','writing_like',.9,'synthetic',0,'v',0,?)", (f'e{i:03}',now))
        c.commit(); c.execute('BEGIN')
        entries = build(c,'a',projection(c,'a',day))
        assert len([x for x in entries if x[0]=='camera_event']) == 101
        assert valid(c,'a',entries) and not valid(c,'b',entries)
        c.rollback()
        c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES('new','a','s','writing_like',.9,'synthetic',0,'v',0,?)", (now,))
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id='s'")
        c.commit(); c.execute('BEGIN')
        assert valid(c,'a',entries)
        c.rollback(); c.execute("UPDATE child_state_event SET confidence=.2 WHERE state_event_id='e100'"); c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries)


def test_changed_or_removed_page_and_pending_deletion_block(synthetic):
    db,day=synthetic
    with db.connect() as c:
        c.execute('BEGIN'); entries=build(c,'a',projection(c,'a',day)); assert any(x[0]=='page' for x in entries); c.rollback()
        c.execute("UPDATE capture_artifact SET extracted_text='altered' WHERE artifact_id='art'"); c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries)
        c.rollback(); c.execute("UPDATE capture_artifact SET extracted_text='1. 求 2+2' WHERE artifact_id='art'"); c.commit(); c.execute('BEGIN')
        assert valid(c,'a',entries); c.rollback()
        c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at) VALUES('d','a','capture_artifact','capture_artifact','art','deleting',?,?)", (utc_now(),utc_now())); c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries)


def test_reject_incomplete_projection_and_malformed_dependencies(synthetic):
    db,day=synthetic
    with db.connect() as c:
        c.execute('BEGIN'); original=projection(c,'a',day)
        tampered=projection(c,'a',day); tampered['visual']['pages'].clear()
        with pytest.raises(ValueError, match='PROJECTION_MISMATCH'): build(c,'a',tampered)
        entries=build(c,'a',original)
        assert not valid(c,'a',entries+entries)
        assert not valid(c,'a',[('page','bad','0'*64)])
        assert not valid(c,'nonexistent',[])
        c.rollback()


def test_question_page_segments_and_document_events_bind_real_rows(synthetic):
    from hashlib import sha256
    from datetime import timedelta
    db,day=synthetic
    start=datetime.fromisoformat(day+'T12:00:00+08:00')
    now=start.isoformat(); end=(start+timedelta(seconds=2)).isoformat()
    with db.connect() as c:
        rev=c.execute("SELECT revision_id,monitor_id FROM document_page_revision WHERE child_id='a'").fetchone()
        revision,monitor=rev[0],rev[1]
        fingerprint=sha256('1.求2+2'.encode()).hexdigest()
        c.execute("INSERT INTO observed_question(question_id,child_id,question_fingerprint,canonical_text,first_revision_id,latest_revision_id,latest_artifact_id,first_seen_at,last_seen_at) VALUES('q','a',?,'1. 求 2+2',?,?,'art',?,?)", (fingerprint,revision,revision,now,now))
        c.execute("INSERT INTO observed_question_occurrence(occurrence_id,question_id,child_id,revision_id,artifact_id,confidence,observed_at) VALUES('occ','q','a',?,'art',.9,?)", (revision,now))
        c.execute("INSERT INTO document_monitor_event(event_id,monitor_id,child_id,session_id,event_type,confidence,metrics_json,observed_at,client_action_id) VALUES('ev',?,'a','s','stable',.9,'{}',?,'event')", (monitor,now))
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('device','document_camera','hash','{}','active')")
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('lease','s','device','document_camera','active',?,?,?)", (now,now,end))
        c.execute("INSERT INTO document_observation_segment VALUES('seg','s','a',?,?,'lease','epoch',1,0,?,?,1000,'hash',?)", (monitor,revision,now,end,now))
        c.commit(); c.execute('BEGIN')
        entries=build(c,'a',projection(c,'a',day))
        assert {'question','segment','document_event'} <= {x[0] for x in entries}
        assert valid(c,'a',entries); c.rollback()
        c.execute("UPDATE observed_question_occurrence SET confidence=.2 WHERE occurrence_id='occ'"); c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries); c.rollback()
        c.execute("UPDATE observed_question_occurrence SET confidence=.9 WHERE occurrence_id='occ'")
        c.execute("DELETE FROM document_observation_segment WHERE segment_id='seg'"); c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries)


def test_camera_correction_blocks_frozen_source(synthetic):
    db,day=synthetic; now=utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES('e','a','s','writing_like',.9,'synthetic',0,'v',0,?)", (now,))
        c.commit(); c.execute('BEGIN'); entries=build(c,'a',projection(c,'a',day)); c.rollback()
        c.execute("INSERT INTO observation_correction(correction_id,child_id,session_id,state_event_id,previous_state,correction_text,effect,occurred_at,client_action_id) VALUES('cor','a','s','e','writing_like','not writing','withdraw',?,'action-correction')", (now,))
        c.commit(); c.execute('BEGIN')
        assert not valid(c,'a',entries)
        refreshed=projection(c,'a',day)
        assert refreshed['samples']['sources']['camera']['withdrawn_count']==1
        rebuilt=build(c,'a',refreshed)
        assert all(not (kind=='camera_event' and source=='e') for kind,source,_ in rebuilt)
        assert valid(c,'a',rebuilt)


def test_rejected_source_aggregate_without_ids_fails_closed(synthetic):
    db,day=synthetic
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='hash mismatch' WHERE artifact_id='art'")
        c.commit(); c.execute('BEGIN')
        data=projection(c,'a',day)
        assert data['visual']['excluded_invalid_count'] > 0
        with pytest.raises(ValueError, match='FROZEN_REJECTED_SOURCES_UNBOUND'):
            build(c,'a',data)

"""Cloud OCR and recorded segments through the real local report services."""
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from home_study_agent.database import Database
from home_study_agent.document_observation_segments import DocumentObservationSegments
from home_study_agent.daily_report_frozen_snapshot import _decrypt
from home_study_agent.daily_report_visual_submission import _render
from home_study_agent.report_delivery import ReportDeliveryService
from test_daily_report_candidate import synthetic
from test_daily_report_frozen_snapshot import freeze


@pytest.mark.parametrize('previous_day', [False, True])
@pytest.mark.parametrize('withdrawal', ['source', 'segment'])
@pytest.mark.parametrize('source_kind', ['cloud', 'local'])
def test_page_segment_survives_freeze_and_reopen_and_binds_source(synthetic, previous_day, withdrawal, source_kind):
    db, day = synthetic
    cloud = {'cloud_vision_fallback': {'status':'needs_review',
        'privacy':{'original_frame_uploaded':False}, 'blocks':[{'text':'地理：世界的气候'}]}}
    with db.connect() as c:
        if source_kind == 'cloud':
            c.execute("UPDATE capture_artifact SET extracted_text='',details_json=? WHERE artifact_id='art'", (json.dumps(cloud,ensure_ascii=False),))
            c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'", (sha256(b'').hexdigest(),))
            c.execute("DELETE FROM observed_question_occurrence WHERE artifact_id='art'")
        monitor = c.execute("SELECT monitor_id,generation,generation_started_at FROM document_monitor_session WHERE session_id='s'").fetchone()
        revision = c.execute("SELECT revision_id FROM document_page_revision WHERE artifact_id='art'").fetchone()[0]
        if previous_day:
            stamp = (datetime.fromisoformat(day+'T00:00:00+08:00') - timedelta(seconds=1)).isoformat()
            c.execute("UPDATE document_page_revision SET created_at=? WHERE revision_id=?", (stamp,revision))
        start = datetime.fromisoformat(monitor['generation_started_at'].replace('Z','+00:00')) + timedelta(seconds=1)
        end = start + timedelta(seconds=10)
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,status) VALUES('d','document_camera','synthetic','{}','active')")
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('l','s','d','document_camera','active',?,?,?)", (start.isoformat(),start.isoformat(),(end+timedelta(minutes=1)).isoformat()))
        c.commit()
    segments = DocumentObservationSegments(db, clock=lambda: end + timedelta(seconds=1))
    assert segments.record('s', child_id='a', revision_id=revision, lease_id='l', epoch=segments.epoch,
        generation=monitor['generation'], seq=0, segment_id='seg', started_at=start.isoformat(),
        ended_at=end.isoformat(), visible_ms=9000)['recorded']
    service, _, snapshot = freeze(db,day)
    with db.connect() as c:
        body = _decrypt(c.execute('SELECT * FROM daily_observation_report_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone(),b's'*32)
        assert body['projection']['timing']['pages'][0]['estimated_visible_ms'] == 9000
        kind = 'cloud_segment' if source_kind == 'cloud' else 'segment'
        assert (kind,'seg') in {(kind,identifier) for kind,identifier,_ in body['dependencies']}
        _, text, _ = _render(day,body,'standard')
        assert ('地理：世界的气候' if source_kind == 'cloud' else '1. 求 2+2') in text
        assert '采样可见约 9 秒' in text
        if previous_day:
            assert body['projection']['visual']['pages'][0]['observations'][0]['observed_at'] == start.isoformat()
    reopened = Database(db.path)
    reopened.initialize()
    assert ReportDeliveryService(reopened,b's'*32).inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']
    with db.connect() as c:
        if withdrawal == 'source':
            if source_kind == 'cloud':
                cloud['cloud_vision_fallback']['blocks'][0]['text'] = 'changed'
                c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='art'",(json.dumps(cloud),))
            else:
                c.execute("UPDATE capture_artifact SET extracted_text='changed' WHERE artifact_id='art'")
        else:
            c.execute("DELETE FROM document_observation_segment WHERE segment_id='seg'")
        c.commit()
    assert not service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']

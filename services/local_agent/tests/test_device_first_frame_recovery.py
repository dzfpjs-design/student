from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.device_registry import DeviceRegistry, UnknownSensorLeaseError
from home_study_agent.repository import utc_now


def setup_registry(tmp_path):
    db = Database(tmp_path / 'study.db'); db.initialize(); now = utc_now()
    with db.connect() as c:
        for child in ('one', 'two'):
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,'初三','江苏南京','active',?,?)", (child, child, now, now))
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES(?,?,?,'study',?,?,?)", (child+'-session', child, 'active' if child == 'one' else 'completed', now, now, child))
        c.commit()
    return db, DeviceRegistry(db)


def states(db):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT child_id,device_type,status,resolved_at FROM device_incident ORDER BY occurred_at')]


@pytest.mark.parametrize('device', ['camera', 'document_camera'])
def test_opening_and_renewing_video_do_not_claim_recovery(tmp_path, device):
    db, registry = setup_registry(tmp_path)
    registry.record_incident('one-session', device, 'NO_USABLE_VIDEO_FRAME', 'no actual frame', 'incident-one')
    lease = registry.register('one-session', device, 'a'*64, {})
    registry.renew(lease['lease_id'])
    assert states(db)[0]['status'] == 'open'
    assert states(db)[0]['resolved_at'] is None
    registry.confirm_frame(lease['lease_id'], 640, 480)
    saved = states(db)
    assert saved[0]['status'] == 'resolved' and saved[0]['resolved_at']
    # A replay and a service restart retain the original recovery timestamp.
    assert DeviceRegistry(db).confirm_frame(lease['lease_id'], 640, 480)['resolved_incidents'] == 0
    assert states(db) == saved


def test_late_old_frame_does_not_erase_a_new_failure_or_another_child(tmp_path):
    db, registry = setup_registry(tmp_path)
    registry.record_incident('one-session', 'camera', 'DEVICE_DISCONNECTED', 'track ended', 'incident-old')
    old = registry.register('one-session', 'camera', 'a'*64, {})
    registry.record_incident('one-session', 'camera', 'NO_USABLE_VIDEO_FRAME', 'no frame', 'incident-new')
    registry.record_incident('two-session', 'camera', 'DEVICE_DISCONNECTED', 'track ended', 'incident-other')
    registry.confirm_frame(old['lease_id'], 640, 480)
    assert [x['status'] for x in states(db)] == ['resolved', 'open', 'open']
    new = registry.register('one-session', 'camera', 'a'*64, {})
    with pytest.raises(UnknownSensorLeaseError): registry.confirm_frame(old['lease_id'], 640, 480)
    registry.confirm_frame(new['lease_id'], 640, 480)
    assert [x['status'] for x in states(db)] == ['resolved', 'resolved', 'open']


@pytest.mark.parametrize('invalid', ['released', 'expired', 'ended_session', 'microphone'])
def test_unusable_or_retired_lease_cannot_resolve_video_incidents(tmp_path, invalid):
    db, registry = setup_registry(tmp_path)
    registry.record_incident('one-session', 'camera', 'DEVICE_DISCONNECTED', 'track ended', 'incident-one')
    device = 'microphone' if invalid == 'microphone' else 'camera'
    lease = registry.register('one-session', device, 'a'*64, {})
    if invalid == 'released': registry.release(lease['lease_id'], 'first_frame_timeout')
    with db.connect() as c:
        if invalid == 'expired': c.execute('UPDATE sensor_lease SET expires_at=? WHERE lease_id=?', ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), lease['lease_id']))
        if invalid == 'ended_session': c.execute("UPDATE daily_session SET status='completed' WHERE session_id='one-session'")
        c.commit()
    before = states(db)
    with pytest.raises(UnknownSensorLeaseError): registry.confirm_frame(lease['lease_id'], 640, 480)
    assert states(db) == before


def test_zero_size_is_not_a_frame(tmp_path):
    db, registry = setup_registry(tmp_path)
    registry.record_incident('one-session', 'camera', 'DEVICE_DISCONNECTED', 'track ended', 'incident-one')
    lease = registry.register('one-session', 'camera', 'a'*64, {})
    with pytest.raises(ValueError): registry.confirm_frame(lease['lease_id'], 0, 480)
    assert states(db)[0]['status'] == 'open'


def test_submillisecond_new_failure_is_not_resolved_by_older_lease(tmp_path):
    db, registry = setup_registry(tmp_path)
    old = registry.record_incident('one-session', 'camera', 'DEVICE_DISCONNECTED', 'track ended', 'incident-old')
    lease = registry.register('one-session', 'camera', 'a'*64, {})
    new = registry.record_incident('one-session', 'camera', 'NO_USABLE_VIDEO_FRAME', 'no frame', 'incident-new')
    base = (datetime.now(timezone.utc)-timedelta(seconds=1)).replace(microsecond=0)
    with db.connect() as c:
        c.execute('UPDATE sensor_lease SET opened_at=? WHERE lease_id=?', ((base+timedelta(microseconds=200)).isoformat(), lease['lease_id']))
        for record, delta in ((old, 100), (new, 300)):
            c.execute('UPDATE device_incident SET occurred_at=? WHERE incident_id=?', ((base+timedelta(microseconds=delta)).isoformat(), record['incident_id']))
            c.execute('UPDATE device_incident_report SET reported_at=? WHERE incident_id=?', ((base+timedelta(microseconds=delta)).isoformat(), record['incident_id']))
        c.commit()
    assert registry.confirm_frame(lease['lease_id'], 640, 480)['resolved_incidents'] == 1
    assert [x['status'] for x in states(db)] == ['resolved', 'open']


def test_repeated_same_failure_after_open_is_not_erased_by_the_old_frame(tmp_path):
    db, registry = setup_registry(tmp_path)
    old = registry.record_incident('one-session', 'camera', 'NO_USABLE_VIDEO_FRAME', 'no frame', 'incident-first')
    lease = registry.register('one-session', 'camera', 'a'*64, {})
    repeated = registry.record_incident('one-session', 'camera', 'NO_USABLE_VIDEO_FRAME', 'no frame again', 'incident-again')
    assert repeated['incident_id'] == old['incident_id']
    assert registry.confirm_frame(lease['lease_id'], 640, 480)['resolved_incidents'] == 0
    assert states(db)[0]['status'] == 'open'
    fresh = registry.register('one-session', 'camera', 'a'*64, {})
    assert registry.confirm_frame(fresh['lease_id'], 640, 480)['resolved_incidents'] == 1

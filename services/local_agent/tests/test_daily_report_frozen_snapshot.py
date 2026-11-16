"""Production freeze in synthetic local DB; never invokes an external adapter."""
import pytest

from home_study_agent.daily_report_observation import prepare
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now
from home_study_agent.database import Database
from local_restore_operation_fixture import restore_local
from test_daily_report_candidate import synthetic


def freeze(db,day,child='a'):
    with db.connect() as c: source = prepare(c,child,day,'recipient-'+child)
    service = ReportDeliveryService(db,b's'*32)
    snapshot = service.freeze_daily_observation(child,day,'recipient-'+child,source)
    return service, source, snapshot['snapshot_id']


def test_encrypted_snapshot_is_stable_for_later_events_and_revoked_for_changes(synthetic):
    db, day = synthetic
    service, source, snapshot = freeze(db,day)
    with db.connect() as c:
        from home_study_agent.daily_report_frozen_snapshot import _decrypt
        row = c.execute('SELECT * FROM daily_observation_report_snapshot').fetchone()
        data = bytes(row['encrypted_payload'])
        assert b'2+2' not in data and '求'.encode() not in data
        assert _decrypt(row,b's'*32)['projection']['visual']['pages'][0]['text'] == '1. 求 2+2'
        c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES('new','a','s','writing_like',.9,'synthetic',0,'synthetic',0,?)", (utc_now(),))
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='s'", (utc_now(),))
        c.commit()
    assert service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']
    reopened = Database(db.path)
    reopened.initialize()
    assert ReportDeliveryService(reopened,b's'*32).inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']
    assert service.freeze_daily_observation('a',day,'recipient-a',source)['snapshot_id'] == snapshot
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='tampered' WHERE artifact_id='art'")
        c.commit()
    assert not service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']
    with pytest.raises(ValueError, match='FROZEN_SOURCE_CHANGED'):
        service.freeze_daily_observation('a',day,'recipient-a',source)


def test_no_key_or_wrong_child_or_tamper_fails_closed(synthetic):
    db, day = synthetic
    with db.connect() as c: source = prepare(c,'a',day,'recipient-a')
    with pytest.raises(ValueError, match='LOCAL_ENCRYPTION_KEY_UNAVAILABLE'):
        ReportDeliveryService(db,None).freeze_daily_observation('a',day,'recipient-a',source)
    service = ReportDeliveryService(db,b's'*32)
    snapshot = service.freeze_daily_observation('a',day,'recipient-a',source)['snapshot_id']
    with pytest.raises(ValueError, match='RECIPIENT_NOT_AUTHORIZED|FROZEN_SNAPSHOT_NOT_FOUND'):
        service.inspect_frozen_daily_observation('b',day,'recipient-a',snapshot)
    with pytest.raises(ValueError, match='FROZEN_SNAPSHOT_UNREADABLE'):
        ReportDeliveryService(db,b'x'*32).inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_report_snapshot SET generated_at='2026-01-01' WHERE snapshot_id=?", (snapshot,)); c.commit()
    with pytest.raises(ValueError, match='FROZEN_SNAPSHOT_UNREADABLE'):
        service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)


def test_snapshot_encrypts_more_than_visible_ocr_preview(synthetic):
    from hashlib import sha256
    from home_study_agent.daily_report_frozen_snapshot import _decrypt
    db, day = synthetic
    text = '一' * 1400 + '题尾内容'
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text=? WHERE artifact_id='art'", (text,))
        c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'", (sha256(text.encode()).hexdigest(),))
        c.commit()
    freeze(db,day)
    with db.connect() as c:
        row = c.execute('SELECT * FROM daily_observation_report_snapshot').fetchone()
        assert '题尾内容'.encode() not in bytes(row['encrypted_payload'])
        body = _decrypt(row,b's'*32)
        assert '题尾内容' not in body['projection']['visual']['pages'][0]['text']
        assert body['details']['pages'][0]['text'].endswith('题尾内容')


def test_unconfirmed_cloud_page_survives_freeze_but_invalidates_on_change(synthetic):
    import json
    from hashlib import sha256
    from home_study_agent.daily_report_frozen_snapshot import _decrypt
    from home_study_agent.daily_report_visual_submission import _render
    db, day = synthetic
    cloud={'cloud_vision_fallback':{'status':'needs_review','privacy':{'original_frame_uploaded':False},
                                  'blocks':[{'text':'地理：世界的气候'}]}}
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='',details_json=? WHERE artifact_id='art'",(json.dumps(cloud,ensure_ascii=False),))
        c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'",(sha256(b'').hexdigest(),))
        c.execute("DELETE FROM observed_question_occurrence WHERE artifact_id='art'")
        c.commit()
    service, _, snapshot=freeze(db,day)
    with db.connect() as c:
        row=c.execute('SELECT * FROM daily_observation_report_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone()
        body=_decrypt(row,b's'*32)
        assert body['details']['pages'][0]['text']=='地理：世界的气候'
        assert '地理：世界的气候'.encode() not in bytes(row['encrypted_payload'])
        _,text,_=_render(day,body,'standard')
        assert '云端页面识别候选，尚未核对' in text and '地理：世界的气候' in text
        cloud['cloud_vision_fallback']['blocks'][0]['text']='changed'
        c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='art'",(json.dumps(cloud),))
        c.commit()
    assert not service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)['current']


def test_recipient_address_and_reconsent_never_revive_old_snapshot(synthetic):
    db, day = synthetic
    service, _, snapshot = freeze(db,day)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_recipient SET address='new@example.test' WHERE recipient_id='recipient-a'")
        c.execute("UPDATE daily_observation_report_outbox SET address_identity='new@example.test' WHERE recipient_id='recipient-a'")
        c.commit()
    with pytest.raises(ValueError, match='FROZEN_RECIPIENT_CHANGED'):
        service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_recipient SET address='parent@example.test' WHERE recipient_id='recipient-a'")
        c.execute("UPDATE daily_observation_report_outbox SET address_identity='parent@example.test' WHERE recipient_id='recipient-a'")
        c.commit()
    service.revoke_recipient('recipient-a')
    service.add_recipient('a','email','parent@example.test','p',True)
    with pytest.raises(ValueError, match='FROZEN_SNAPSHOT_NOT_FOUND'):
        service.inspect_frozen_daily_observation('a',day,'recipient-a',snapshot)


def test_deletion_purges_encrypted_snapshot(synthetic,tmp_path):
    db, day = synthetic
    freeze(db,day)
    privacy = PrivacyLifecycleService(db,tmp_path,b's'*32)
    privacy.initialize()
    privacy.create_backup(prune=False)
    assert privacy.delete_capture_artifact('art','确认删除','freeze-delete')['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_report_snapshot').fetchone()[0] == 0


def test_restore_quarantines_frozen_snapshot(synthetic,tmp_path):
    db, day = synthetic
    freeze(db,day)
    privacy = PrivacyLifecycleService(db,tmp_path,b's'*32)
    privacy.initialize()
    backup = privacy.create_backup(prune=False)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=last_checkpoint_at WHERE session_id='s'")
        c.commit()
    assert restore_local(privacy, backup['backup_id'], '确认恢复')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_report_snapshot').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM daily_observation_report_source').fetchone()[0] == 0


def test_delete_waits_for_local_freeze_and_removes_result(synthetic,tmp_path,monkeypatch):
    from threading import Event, Thread
    import home_study_agent.daily_report_frozen_snapshot as frozen
    db, day = synthetic
    with db.connect() as c: source = prepare(c,'a',day,'recipient-a')
    privacy = PrivacyLifecycleService(db,tmp_path,b's'*32)
    privacy.initialize()
    entered, release, deletion_finished = Event(), Event(), Event()
    errors = []
    real_projection = frozen._projection
    def pause_projection(*args):
        entered.set()
        assert release.wait(5)
        return real_projection(*args)
    monkeypatch.setattr(frozen,'_projection',pause_projection)
    def freezing():
        try: ReportDeliveryService(db,b's'*32).freeze_daily_observation('a',day,'recipient-a',source)
        except Exception as error: errors.append(error)
    def deleting():
        try: privacy.delete_capture_artifact('art','确认删除','parallel-delete')
        except Exception as error: errors.append(error)
        finally: deletion_finished.set()
    first, second = Thread(target=freezing), Thread(target=deleting)
    first.start()
    assert entered.wait(5)
    second.start()
    try: assert not deletion_finished.wait(.2)
    finally: release.set()
    first.join(10); second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_report_snapshot').fetchone()[0] == 0

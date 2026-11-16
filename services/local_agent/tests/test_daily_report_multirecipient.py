"""Synthetic recipient-specific fixed tests; in-memory fake adapter only."""
import pytest

from home_study_agent.report_delivery import ChannelTestActionConflictError, DeliveryStateError
from test_daily_report_candidate import synthetic
from test_daily_report_email_verification import Fake, check, setup


def test_explicit_second_email_and_old_default_are_exact_and_replay_safe(synthetic):
    db,service,fake = setup(synthetic)
    second = service.add_recipient('a','email','second@example.test','第二位',True)['recipient_id']
    result=service.test_channel('a','email','explicit-second',second)
    assert result['status']=='passed' and result['recipient_id']==second
    assert fake.calls == ['second@example.test']
    assert check(db,'a',second) and not check(db,'a','recipient-a')
    assert service.test_channel('a','email','explicit-second',second)['status']=='passed'
    with pytest.raises(ChannelTestActionConflictError):
        service.test_channel('a','email','explicit-second','recipient-a')
    assert fake.calls == ['second@example.test']
    assert service.test_channel('a','email','legacy-default')['status']=='passed'
    assert fake.calls == ['second@example.test','parent@example.test']
    assert check(db,'a','recipient-a') and check(db,'a',second)


def test_other_child_and_revoked_recipient_never_send(synthetic):
    _,service,fake=setup(synthetic)
    with pytest.raises(DeliveryStateError):
        service.test_channel('a','email','wrong-child','recipient-b')
    second=service.add_recipient('a','email','second@example.test','第二位',True)['recipient_id']
    service.revoke_recipient(second)
    with pytest.raises(DeliveryStateError):
        service.test_channel('a','email','revoked-second',second)
    assert fake.calls==[]


def test_unknown_reissue_cannot_change_recipient_or_authority(synthetic,monkeypatch):
    db,service,_=setup(synthetic)
    second=service.add_recipient('a','email','second@example.test','第二位',True)['recipient_id']
    uncertain=Fake(unknown=True); service.adapters['email']=uncertain
    result=service.test_channel('a','email','unknown-second',second)
    assert result['status']=='unknown' and result['recipient_id']==second
    assert service.channel_test_status('a')[0]['recipient_id']==second
    service.keep_unknown_channel_test('a','email','unknown-second')
    with pytest.raises(ChannelTestActionConflictError):
        service.reissue_unknown_channel_test('a','email','unknown-second','wrong-retry','recipient-a')
    # A re-save is a new authority generation even if the wall clock repeats.
    import home_study_agent.report_delivery as module
    monkeypatch.setattr(module,'utc_now',lambda:'2026-11-04T00:00:00+00:00')
    service.add_recipient('a','email','second@example.test','第二位',True)
    with pytest.raises(DeliveryStateError,match='授权已变化'):
        service.reissue_unknown_channel_test('a','email','unknown-second','new-retry',second)
    with db.connect() as c:
        assert c.execute("SELECT status FROM report_delivery_channel_test_action WHERE client_action_id='unknown-second'").fetchone()[0]=='unknown_acknowledged'
    assert uncertain.calls == ['second@example.test']


def test_unknown_explicit_reissue_keeps_exact_original_target(synthetic):
    _,service,_=setup(synthetic)
    second=service.add_recipient('a','email','second@example.test','第二位',True)['recipient_id']
    service.adapters['email']=Fake(unknown=True)
    assert service.test_channel('a','email','unknown-second',second)['status']=='unknown'
    service.keep_unknown_channel_test('a','email','unknown-second')
    fake=Fake(); service.adapters['email']=fake
    result=service.reissue_unknown_channel_test('a','email','unknown-second','explicit-retry',second)
    assert result['status']=='passed' and result['recipient_id']==second
    assert fake.calls == ['second@example.test']
    service.reissue_unknown_channel_test('a','email','unknown-second','explicit-retry',second)
    assert fake.calls == ['second@example.test']
    service.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'changed@example.test'})
    historical=service.reissue_unknown_channel_test('a','email','unknown-second','explicit-retry',second)
    assert historical['status']=='passed' and historical['historical_only'] is True
    assert historical['current_verified'] is False and fake.calls == ['second@example.test']


def test_legacy_unknown_without_original_recipient_cannot_be_reissued(synthetic):
    from home_study_agent.repository import utc_now
    db,service,fake=setup(synthetic)
    with db.connect() as c:
        c.execute("INSERT INTO report_delivery_channel_test_action(client_action_id,child_id,channel,status,created_at) VALUES('legacy-unknown','a','email','unknown',?)", (utc_now(),))
        c.commit()
    with pytest.raises(DeliveryStateError,match='收件人身份'):
        service.reissue_unknown_channel_test('a','email','legacy-unknown','legacy-retry')
    assert fake.calls == []


def test_real_app_http_specifies_recipient_with_parent_auth(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'api.db',maintenance_enabled=False,
                      session_token='synthetic-local',data_encryption_key=b's'*32)
    with TestClient(create_app(settings),client=('127.0.0.1',4200)) as client:
        local={'X-HSC-Session':'synthetic-local'}
        parent=client.post('/v1/parent/setup',headers=local,json={'pin':'2468'}).json()['parent_token']
        headers={**local,'X-HSC-Parent':parent}
        service=client.app.state.report_delivery
        first=service.add_recipient('child-primary-4','email','first@example.test','第一位',True)['recipient_id']
        second=service.add_recipient('child-primary-4','email','second@example.test','第二位',True)['recipient_id']
        service.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'sender@example.test'})
        fake=Fake(); service.adapters['email']=fake
        path='/v1/parent/children/child-primary-4/report-delivery/channels/email/test'
        assert client.post(path,headers=local,json={'client_action_id':'explicit-action','recipient_id':second}).status_code==401
        result=client.post(path,headers=headers,json={'client_action_id':'explicit-action','recipient_id':second})
        assert result.status_code==200 and result.json()['recipient_id']==second
        assert fake.calls==['second@example.test']
        assert client.post(path,headers=headers,json={'client_action_id':'explicit-action','recipient_id':first}).status_code==409
        default=client.post(path,headers=headers,json={'client_action_id':'legacy-action'})
        assert default.status_code==200 and default.json()['recipient_id']==first
        assert fake.calls==['second@example.test','first@example.test']
        service.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'changed@example.test'})
        historical=client.post(path,headers=headers,json={'client_action_id':'explicit-action','recipient_id':second})
        assert historical.status_code==200 and historical.json()['status']=='blocked'
        assert historical.json()['historical_status']=='passed' and historical.json()['current_verified'] is False
        assert fake.calls==['second@example.test','first@example.test']
        with service.database.connect() as c:
            selection=c.execute("SELECT summary_json FROM audit_event WHERE action='report_delivery_test_recipient_selected'").fetchall()
            assert selection and all('example.test' not in row[0] for row in selection)

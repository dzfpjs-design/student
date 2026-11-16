"""Synthetic scheduler candidate; never use a real SMTP server."""
from datetime import date, datetime, time, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
from home_study_agent import daily_report_visual_schedule as schedule
from home_study_agent import daily_report_visual_submission as submission
from test_daily_report_candidate import synthetic
from test_daily_report_email_verification import setup
from test_daily_visual_submission import FakeSMTP


@pytest.fixture
def env(synthetic, monkeypatch):
    db, day = synthetic
    with db.connect() as c:
        c.execute(schedule.SCHEDULE_SCHEMA)
        c.commit()
    _, service, _ = setup(synthetic)
    service.save_channel_config('email', {'host':'127.0.0.1', 'port':25,
        'from_address':'synthetic@example.test','allow_external_delivery':True})
    assert service.test_channel('a','email','synthetic-verify','recipient-a')['status'] == 'passed'
    instant = [datetime.combine(date.fromisoformat(day),time(4),timezone.utc)]
    monkeypatch.setattr(schedule,'_utc_now',lambda: instant[0])
    monkeypatch.setattr(submission,'_utc_now',lambda: instant[0])
    fake = FakeSMTP(db)
    service.adapters['email'] = fake
    return db, day, service, fake, instant


def test_first_enable_today_no_reset_and_complete_days_recover(env):
    db, day, service, fake, instant = env
    assert schedule.enable(service,'a') == {'start_day':day,'next_day':day,'created':True}
    assert schedule.run_due(service,'a')['status']=='waiting_for_day_end'
    instant[0] += timedelta(days=3)
    assert schedule.enable(service,'a')['start_day'] == day
    result=schedule.run_due(service,'a',max_days=2)
    assert result['status']=='more_days_pending' and len(fake.calls)==2, result
    assert schedule.run_due(service,'a')['status']=='waiting_for_day_end'
    assert len(fake.calls)==3
    assert schedule.run_due(service,'a')['status']=='waiting_for_day_end' and len(fake.calls)==3
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_delivery_ledger WHERE status=?',('sent',)).fetchone()[0]==3


def test_quiet_and_unverified_recipient_do_not_advance(env):
    db, day, service, fake, instant = env
    schedule.enable(service,'a')
    instant[0] += timedelta(days=1,hours=11)
    assert schedule.run_due(service,'a')['reason']=='QUIET_HOURS'
    assert not fake.calls
    instant[0] -= timedelta(hours=11)
    with db.connect() as c:
        c.execute('DELETE FROM daily_report_email_verification WHERE recipient_id=?',('recipient-a',)); c.commit()
    result=schedule.run_due(service,'a')
    assert result['reason']=='RECIPIENT_EMAIL_TEST_REQUIRED' and result['next_day']==day
    assert not fake.calls


def test_unknown_result_blocks_cursor_and_retry(env):
    db, day, service, _, instant = env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    fake=FakeSMTP(db,outcome='unknown'); service.adapters['email']=fake
    assert schedule.run_due(service,'a')['reason']=='DELIVERY_NOT_CONFIRMED'
    assert schedule.run_due(service,'a')['reason']=='DELIVERY_RESULT_UNKNOWN'
    assert len(fake.calls)==1
    with db.connect() as c:
        assert c.execute('SELECT next_day FROM daily_visual_schedule').fetchone()[0]==day


def test_formal_report_blocks_visual_only_sending(env):
    db,day,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    with db.connect() as c:
        c.execute("INSERT INTO daily_report VALUES('formal','s','a',?,'synthetic','{}','{}','{}','synthetic')",(day,)); c.commit()
    result=schedule.run_due(service,'a')
    assert result['reason']=='FORMAL_REPORT_MERGE_REQUIRED' and result['next_day']==day
    assert not fake.calls


def test_missing_authorization_does_not_advance(env):
    db,day,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    service.revoke_recipient('recipient-a')
    result=schedule.run_due(service,'a')
    assert result['reason']=='RECIPIENT_AUTHORIZATION_REQUIRED' and result['next_day']==day
    assert not fake.calls


def test_pending_second_recipient_blocks_day_before_any_send(env):
    db,day,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    service.add_recipient('a','email','second@example.test','second',False)
    result=schedule.run_due(service,'a')
    assert result['reason']=='RECIPIENT_AUTHORIZATION_REQUIRED' and result['next_day']==day
    assert not fake.calls


def test_restore_barrier_and_disabled_schedule_never_advance(env):
    db,day,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_barrier SET blocked=1,reason='RESTORE_HISTORY_UNRECONCILED'"); c.commit()
    assert schedule.run_due(service,'a')['reason']=='RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED'
    with db.connect() as c:
        c.execute('UPDATE daily_visual_schedule SET enabled=0'); c.commit()
    with pytest.raises(ValueError,match='DISABLED_REQUIRES_REVIEW'): schedule.enable(service,'a')
    with pytest.raises(ValueError,match='NOT_ENABLED'): schedule.run_due(service,'a')
    assert not fake.calls


def test_sent_receipt_survives_cursor_write_failure(env):
    db,day,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    with db.connect() as c:
        c.execute("CREATE TRIGGER synthetic_cursor_failure BEFORE UPDATE ON daily_visual_schedule BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"); c.commit()
    with pytest.raises(Exception,match='synthetic failure'): schedule.run_due(service,'a')
    assert len(fake.calls)==1
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='sent'
        assert c.execute('SELECT next_day FROM daily_visual_schedule').fetchone()[0]==day
        c.execute('DROP TRIGGER synthetic_cursor_failure'); c.commit()
    assert schedule.run_due(service,'a')['status']=='waiting_for_day_end'
    assert len(fake.calls)==1


def test_parallel_maintenance_ticks_send_once(env):
    _,_,service,fake,instant=env
    schedule.enable(service,'a'); instant[0] += timedelta(days=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:schedule.run_due(service,'a'),range(2)))
    assert all(result['status']=='waiting_for_day_end' for result in outcomes)
    assert len(fake.calls)==1

"""Invalid historical dates are unknown, never proof of a different day."""
import pytest

from home_study_agent.daily_report_observation import _authority, _legacy_status
from home_study_agent.report_delivery import DeliveryStateError
from home_study_agent.repository import utc_now
from test_daily_report_candidate import synthetic
from test_daily_report_legacy_delivery_barrier import legacy
from test_report_auto_recipient_verification import setup


@pytest.mark.parametrize('legacy_day',['legacy-unknown-date','2026-2-03','20260203','2026-02-30',''])
@pytest.mark.parametrize('status',['sent','unknown'])
def test_visual_preparation_treats_invalid_legacy_date_as_unknown(synthetic,legacy_day,status):
    db,day=synthetic
    legacy(db,legacy_day,status)
    with db.connect() as c:
        assert _legacy_status(c,'a',day,'parent@example.test')=='unknown'
        with pytest.raises(ValueError,match='FORMAL_DELIVERY_EXISTS'):
            _authority(c,'a',day,'recipient-a')
        assert _legacy_status(c,'a',day,'different@example.test') is None
        assert _legacy_status(c,'b',day,'parent@example.test') is None


def test_visual_legacy_valid_other_day_does_not_block(synthetic):
    db,day=synthetic
    previous='2020-01-01' if day != '2020-01-01' else '2020-01-02'
    legacy(db,previous,'sent')
    with db.connect() as c:
        assert _legacy_status(c,'a',day,'parent@example.test') is None


@pytest.mark.parametrize('legacy_day',['legacy-unknown-date','2026-2-03','20260203','2026-02-30',''])
@pytest.mark.parametrize('status',['sent','unknown'])
def test_formal_auto_queue_blocks_invalid_prior_date(tmp_path,legacy_day,status):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    assert delivery.test_channel(child,'email','test-email-a',a)['status']=='passed'
    reports.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        original=c.execute('SELECT * FROM daily_report WHERE report_id=?',(snapshot['report_id'],)).fetchone()
        session=c.execute('SELECT * FROM daily_session WHERE session_id=?',(original['session_id'],)).fetchone()
        values=dict(session); values.update(session_id='s2',client_action_id='s2-action')
        cols=list(values); c.execute('INSERT INTO daily_session ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[values[k] for k in cols])
        values=dict(original); values.update(report_id='r2',session_id='s2')
        cols=list(values); c.execute('INSERT INTO daily_report ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[values[k] for k in cols])
        values=dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot['snapshot_id'],)).fetchone())
        values.update(snapshot_id='snap2',source_id='r2',report_id='r2')
        cols=list(values); c.execute('INSERT INTO parent_report_snapshot ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',[values[k] for k in cols])
        now=utc_now()
        c.execute("INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('prior',?,?,'email','auto:prior',?,?,?,?)",(snapshot['snapshot_id'],a,status,now,now,now))
        c.execute('UPDATE daily_report SET report_date=? WHERE report_id=?',(legacy_day,snapshot['report_id']))
        c.commit()
    with pytest.raises(DeliveryStateError):
        delivery.enqueue('snap2',a,automatic=True)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM report_delivery_outbox WHERE snapshot_id='snap2'").fetchone()[0] == 0
    assert len(fake.messages)==1  # Only the fake fixed verification.

"""Persisted synthetic revision/receipt history; no transport is invoked."""
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_daily_report_evidence_versions import CHILD, flow


def revision(flow, title):
    db, repo, report, service = flow
    current = repo.get_daily_report(report['report_id'])
    # Model a persisted source revision; save_daily_report is insert-idempotent,
    # so calling it again cannot produce a new historical source version.
    with db.connect() as c:
        c.execute('UPDATE daily_report SET summary_json=? WHERE report_id=?',
            (json.dumps({**current['summary'],'headline':title}),current['report_id']))
        c.commit()
    snapshot = service.snapshots.create_from_daily(repo.get_daily_report(current['report_id']))
    return service.snapshots.approve(snapshot['snapshot_id'])


def receipt(flow, snapshot, recipient, status):
    db, _, _, service = flow
    queued = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    # Explicit historical transport fixture, not a claim of SMTP verification.
    with db.connect() as c:
        c.execute('UPDATE report_delivery_outbox SET status=? WHERE delivery_id=?',(status,queued['delivery_id']))
        c.commit()
    return queued['delivery_id']


@pytest.fixture
def chain(flow):
    db, _, _, service = flow
    mom = service.add_recipient(CHILD,'email','mom@local.test','Synthetic A',True)
    dad = service.add_recipient(CHILD,'email','dad@local.test','Synthetic B',True)
    first = revision(flow,'first synthetic version')
    receipt(flow,first,mom,'sent')
    receipt(flow,first,dad,'unknown')
    second = revision(flow,'second synthetic version')
    receipt(flow,second,mom,'sent')
    third = revision(flow,'third synthetic version')
    assert len({s['snapshot_id'] for s in (first,second,third)})==3
    receipt(flow,third,dad,'sent')
    return db,service,first,second,third,mom,dad


def test_latest_detail_carries_the_same_two_recipient_obligation_as_history(chain):
    db, service, first, _, third, mom, dad = chain
    historical = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    latest = service.snapshots.correction_status(CHILD,third['snapshot_id'])
    assert latest['recipient_ids_requiring_correction']==historical['recipient_ids_requiring_correction']==[mom['recipient_id']]
    assert latest['latest_replacement_id']==third['snapshot_id']
    assert {r['recipient_id'] for r in latest['prior_submissions']}=={mom['recipient_id'],dad['recipient_id']}
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert service.snapshots.correction_status(CHILD,third['snapshot_id'],connection=c)==latest
        assert c.total_changes==0
    assert ParentReportService(Database(db.path)).correction_status(CHILD,third['snapshot_id'])==latest


@pytest.mark.parametrize('disruption',['backdated_audit','deleted_intermediate'])
def test_latest_survives_clock_order_and_deleted_intermediate(chain,disruption):
    db, service, first, second, third, mom, _ = chain
    with db.connect() as c:
        if disruption=='backdated_audit':
            c.execute("""UPDATE audit_event SET occurred_at='2000-01-01T00:00:00Z'
                WHERE action='parent_daily_snapshot_superseded' AND json_extract(summary_json,'$.snapshot_id')=?""",(second['snapshot_id'],))
        else:
            c.execute('DELETE FROM report_delivery_outbox WHERE snapshot_id=?',(second['snapshot_id'],))
            c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(second['snapshot_id'],))
        c.commit()
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['latest_replacement_id']==third['snapshot_id']
    assert state['latest_replacement_availability']=='available'
    assert state['recipient_ids_requiring_correction']==[mom['recipient_id']]


def test_deleted_latest_never_falls_back_to_an_older_sent_version(chain):
    db, service, first, second, third, mom, dad = chain
    with db.connect() as c:
        c.execute('DELETE FROM report_delivery_outbox WHERE snapshot_id=?',(third['snapshot_id'],))
        c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(third['snapshot_id'],))
        c.commit()
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['latest_replacement_id'] is None
    assert state['latest_replacement_availability']=='deleted'
    assert set(state['recipient_ids_requiring_correction'])=={mom['recipient_id'],dad['recipient_id']}
    assert not any(r['is_latest_replacement'] for r in state['replacement_submissions'])
    assert state['correction_notification_required']


def test_revoked_consent_blocks_notification_but_erasure_does_not_resurrect_recipient(chain):
    db, service, first, _, third, mom, dad = chain
    service.revoke_recipient(mom['recipient_id'])
    service.revoke_recipient(dad['recipient_id'])
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['recipient_ids_requiring_correction']==[mom['recipient_id']]
    assert state['recipient_ids_blocked_by_consent']==[mom['recipient_id']]
    assert state['correction_notification_required']
    with db.connect() as c:
        c.execute('DELETE FROM report_delivery_outbox WHERE recipient_id=?',(mom['recipient_id'],))
        c.execute('DELETE FROM report_delivery_recipient WHERE recipient_id=?',(mom['recipient_id'],))
        c.commit()
    erased = service.snapshots.correction_status(CHILD,third['snapshot_id'])
    assert mom['recipient_id'] not in json.dumps(erased)
    assert erased['recipient_ids_requiring_correction']==[]
    assert not erased['correction_notification_required']


def test_unrelated_same_child_audit_link_cannot_pay_a_correction_obligation(chain,flow):
    db, service, first, _, third, mom, _ = chain
    unrelated = service.snapshots.create_from_period(PeriodReportService(db).generate(CHILD,'monthly'))
    unrelated = service.snapshots.approve(unrelated['snapshot_id'])
    receipt(flow,unrelated,mom,'sent')
    with db.connect() as c:
        c.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES (?,'system','parent_daily_snapshot_superseded',?,?,'2099-01-01T00:00:00Z')""",
            (str(uuid4()),CHILD,json.dumps({'snapshot_id':first['snapshot_id'],'replacement_snapshot_id':unrelated['snapshot_id']})))
        c.commit()
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['latest_replacement_id']==third['snapshot_id']
    assert state['recipient_ids_requiring_correction']==[mom['recipient_id']]
    assert unrelated['snapshot_id'] not in json.dumps(state)


def test_explicitly_revoked_predecessor_still_has_a_same_source_successor(flow):
    _, _, _, service = flow
    first = revision(flow,'old explicitly withdrawn')
    service.snapshots.revoke(first['snapshot_id'],'parent withdrew obsolete version')
    second = revision(flow,'new after withdrawal')
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['latest_replacement_id']==second['snapshot_id']
    assert state['latest_replacement_availability']=='available'


@pytest.mark.parametrize('invalidation',['revoke','source_changed_without_refresh'])
def test_revoked_latest_includes_recipient_who_only_received_that_version(chain,flow,invalidation):
    db, service, first, _, third, _, _ = chain
    other = service.add_recipient(CHILD,'email','third@local.test','Synthetic C',True)
    receipt(flow,third,other,'sent')
    if invalidation=='revoke':
        service.snapshots.revoke(third['snapshot_id'],'parent withdrew current report')
    else:
        with db.connect() as c:
            c.execute("UPDATE daily_report SET summary_json=json_set(summary_json,'$.headline','source changed') WHERE report_id=?",(third['report_id'],))
            c.commit()
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert other['recipient_id'] in state['recipient_ids_requiring_correction']
    assert not any(r['source_current'] for r in state['replacement_submissions'])


def test_new_current_revision_recovers_after_latest_was_deleted(chain,flow):
    db, service, first, _, third, _, _ = chain
    with db.connect() as c:
        c.execute('DELETE FROM report_delivery_outbox WHERE snapshot_id=?',(third['snapshot_id'],))
        c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(third['snapshot_id'],))
        c.commit()
    fourth = revision(flow,'new current after latest deletion')
    state = service.snapshots.correction_status(CHILD,first['snapshot_id'])
    assert state['latest_replacement_id']==fourth['snapshot_id']
    assert state['latest_replacement_availability']=='available'


def test_limited_list_uses_newest_insertion_when_snapshot_timestamps_tie(chain):
    db, service, _, _, third, _, _ = chain
    with db.connect() as c:
        c.execute("UPDATE parent_report_snapshot SET created_at='2026-01-01T00:00:00Z' WHERE child_id=?",(CHILD,))
        c.commit()
    assert service.snapshots.list(CHILD,limit=1)[0]['snapshot_id']==third['snapshot_id']

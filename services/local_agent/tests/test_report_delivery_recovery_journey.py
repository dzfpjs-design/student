"""Synthetic SQLite and real loopback SMTP, never external report delivery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import socketserver
import threading

import pytest
from normal_provider_owner import normal_provider_owner
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.parent_reports import SnapshotStateError
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from test_daily_report_evidence_versions import CHILD, ITEM, flow
from test_companion_activity_host import NoCloud


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 loopback synthetic SMTP\r\n')
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.split(b' ', 1)[0].strip().upper()
            if command in {b'EHLO', b'HELO', b'MAIL', b'RCPT', b'RSET'}:
                self.wfile.write(b'250 OK\r\n')
            elif command == b'DATA':
                self.wfile.write(b'354 End with dot\r\n')
                data = []
                while True:
                    part = self.rfile.readline()
                    if not part:
                        return
                    if part == b'.\r\n':
                        break
                    data.append(part)
                self.server.submissions.append(b''.join(data))
                if self.server.mode == 'unknown':
                    return  # DATA received, acknowledgement lost.
                if self.server.mode == 'failed':
                    self.wfile.write(b'451 temporary rejection\r\n')
                else:
                    self.wfile.write(b'250 accepted\r\n')
            elif command == b'QUIT':
                self.wfile.write(b'221 bye\r\n')
                return
            else:
                self.wfile.write(b'502 unsupported\r\n')


@pytest.fixture
def smtp():
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), SMTPHandler)
    server.mode, server.submissions = 'sent', []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def prepare(flow, smtp, report_type, *, periods=None):
    db, repo, report, service = flow
    if report_type == 'daily':
        snapshot = service.snapshots.create_from_daily(report)
    else:
        source = (periods or PeriodReportService(db)).generate(CHILD, report_type)
        snapshot = service.snapshots.create_from_period(source)
    service.snapshots.approve(snapshot['snapshot_id'])
    recipient = service.add_recipient(CHILD, 'email', 'synthetic@local.test', 'Synthetic', True)
    hour = datetime.now().hour
    service.update_preferences(CHILD, f'{(hour+1)%24:02d}:00', f'{(hour+2)%24:02d}:00', 90)
    service.save_channel_config('email', {'host':'127.0.0.1', 'port':smtp.server_address[1],
        'from_address':'synthetic@local.test', 'starttls':False, 'allow_external_delivery':True})
    return snapshot, recipient


@pytest.mark.parametrize('report_type', ['daily', 'weekly', 'monthly'])
@pytest.mark.parametrize('transport', ['sent', 'failed', 'unknown'])
def test_cross_minute_retrigger_after_protocol_result_and_restart_never_creates_a_second_send(
        flow, smtp, monkeypatch, report_type, transport, normal_provider_owner):
    db, _, _, service = flow
    snapshot, recipient = prepare(flow, smtp, report_type)
    smtp.mode = transport
    queued = service.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    result = service.process_due()[0]
    assert result['status'] == ('scheduled' if transport == 'failed' else transport)
    assert len(smtp.submissions) == 1
    assert not result['human_delivery_verified'] and not result['human_read_verified']
    restarted = ReportDeliveryService(Database(db.path), b'D'*32)
    restarted.initialize()
    later = (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
    monkeypatch.setattr('home_study_agent.report_delivery.utc_now', lambda: later)
    repeated = restarted.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    assert repeated['delivery_id'] == queued['delivery_id']
    assert repeated['attempts'] == result['attempts']
    assert repeated['scheduled_at'] == result['scheduled_at']
    assert restarted.process_due() == []
    assert len(smtp.submissions) == 1
    if transport == 'unknown':
        with pytest.raises(DeliveryStateError):
            restarted.enqueue(snapshot['snapshot_id'], recipient['recipient_id'], later)
    assert len(restarted.list_deliveries(snapshot['snapshot_id'])) == 1


def test_concurrent_initial_enqueue_and_processing_cannot_bypass_explicit_reissue(flow,smtp):
    db, _, _, service = flow
    snapshot, recipient = prepare(flow, smtp, 'daily')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.enqueue(snapshot['snapshot_id'],recipient['recipient_id']),range(2)))
    assert len({r['delivery_id'] for r in results}) == 1
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='processing' WHERE delivery_id=?",(results[0]['delivery_id'],))
        c.commit()
    with pytest.raises(DeliveryStateError):
        service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'],'2099-01-01T00:00:00+00:00')


@pytest.mark.parametrize('report_type', ['daily', 'weekly', 'monthly'])
@pytest.mark.parametrize('transport', ['sent', 'unknown'])
def test_parent_snapshot_journey_reconciles_withdrawn_sources_and_keeps_submission_history(
        flow,smtp,report_type,transport, normal_provider_owner):
    db, _, _, service = flow
    snapshot, recipient = prepare(flow,smtp,report_type)
    smtp.mode = transport
    queued = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    sent = service.process_due()[0]
    assert sent['status'] == transport and len(smtp.submissions) == 1
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(ITEM,))
        c.commit()
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        pending = service.snapshots.correction_status(CHILD,snapshot['snapshot_id'],connection=c)
        assert not pending['source_current'] and pending['correction_notification_required']
        assert c.total_changes == before
    if transport == 'unknown':
        with pytest.raises(DeliveryStateError):
            service.reissue_unknown(queued['delivery_id'],'stale-source-reissue')
    # Use the existing parent's list/get path, not an unconnected reconciliation helper.
    snapshots = service.snapshots.list(CHILD)
    previous = service.snapshots.get(snapshot['snapshot_id'])
    assert previous['status'] == 'revoked'
    correction = previous['correction_status']
    assert correction['correction_notification_required'] is True
    replacement_id = correction['replacements'][-1]['replacement_snapshot_id']
    replacement = next(s for s in snapshots if s['snapshot_id']==replacement_id)
    assert replacement['status']=='draft' and replacement['approved_at'] is None
    assert replacement['evidence_version'] != snapshot['evidence_version']
    assert correction['prior_submissions'][0]['delivery_id']==queued['delivery_id']
    assert correction['prior_submissions'][0]['recipient_id']==recipient['recipient_id']
    assert correction['prior_submissions'][0]['status']==transport
    assert service.get_delivery(queued['delivery_id'])['attempts']==sent['attempts']
    restarted = ReportDeliveryService(Database(db.path), b'D'*32)
    restarted.initialize()
    assert restarted.snapshots.get(snapshot['snapshot_id'])['correction_status']==correction
    assert restarted.process_due()==[] and len(smtp.submissions)==1
    with pytest.raises(KeyError):
        restarted.snapshots.correction_status('child-junior-3',snapshot['snapshot_id'])
    # A new reviewed source version is a distinct authorized correction, never
    # a silent retry of the old unknown report.
    smtp.mode = 'sent'
    restarted.snapshots.approve(replacement_id)
    replacement_delivery = restarted.enqueue(replacement_id,recipient['recipient_id'])
    assert restarted.process_due()[0]['status']=='sent'
    assert len(smtp.submissions)==2 and smtp.submissions[0] != smtp.submissions[1]
    reconciled = restarted.snapshots.get(snapshot['snapshot_id'])['correction_status']
    assert reconciled['replacement_submissions'][0]['delivery_id']==replacement_delivery['delivery_id']
    assert reconciled['replacement_submissions'][0]['snapshot_id']==replacement_id
    assert reconciled['replacement_submissions'][0]['recipient_id']==recipient['recipient_id']
    assert reconciled['replacement_submissions'][0]['evidence_version']==replacement['evidence_version']
    assert reconciled['replacement_submissions'][0]['channel_acceptance']=='accepted'
    assert not reconciled['replacement_submissions'][0]['human_delivery_verified']
    assert reconciled['correction_notification_required'] is False
    assert reconciled['next_action']=='verify_correction_receipt'
    assert reconciled['prior_submissions'][0]['status']==transport
    assert ReportDeliveryService(Database(db.path),b'D'*32).snapshots.get(
        snapshot['snapshot_id'])['correction_status']==reconciled


def test_preexisting_scheduled_duplicate_cannot_send_after_prior_submission_became_unknown(flow,smtp, normal_provider_owner):
    _, _, _, service = flow
    snapshot, recipient = prepare(flow,smtp,'daily')
    first = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    later = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'],'2000-01-01T00:00:00+00:00')
    smtp.mode = 'unknown'
    assert service._process_one(first['delivery_id'])['status']=='unknown'
    result = service._process_one(later['delivery_id'])
    assert len(smtp.submissions)==1
    assert result['status']=='cancelled' and result['cancel_reason']=='prior_delivery_unresolved'


@pytest.mark.parametrize('legacy_time_key',[False,True])
def test_pending_queue_restart_reuses_original_schedule_and_legacy_identity(flow,smtp,legacy_time_key, normal_provider_owner):
    db, _, _, service = flow
    snapshot, recipient = prepare(flow,smtp,'weekly')
    args = ('2000-01-01T00:00:00+00:00',) if legacy_time_key else ()
    original = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'],*args)
    restarted = ReportDeliveryService(Database(db.path),b'D'*32)
    restarted.initialize()
    repeated = restarted.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    assert repeated==original
    smtp.mode = 'unknown'
    assert restarted.process_due()[0]['status']=='unknown'
    assert len(smtp.submissions)==1
    again = ReportDeliveryService(Database(db.path),b'D'*32)
    again.initialize()
    assert again.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])['delivery_id']==original['delivery_id']
    assert again.process_due()==[] and len(smtp.submissions)==1


def test_explicit_reissue_does_not_release_an_old_pending_duplicate(flow,smtp, normal_provider_owner):
    db, _, _, service = flow
    snapshot, recipient = prepare(flow,smtp,'daily')
    first = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    later = service.enqueue(snapshot['snapshot_id'],recipient['recipient_id'],'2099-01-01T00:00:00+00:00')
    smtp.mode = 'unknown'
    assert service.process_due()[0]['status']=='unknown'
    restarted = ReportDeliveryService(Database(db.path),b'D'*32)
    restarted.initialize()
    replacement = restarted.reissue_unknown(first['delivery_id'],'explicit-single-reissue')
    smtp.mode = 'sent'
    assert restarted.process_due()[0]['delivery_id']==replacement['delivery_id']
    assert len(smtp.submissions)==2
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET scheduled_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?",(later['delivery_id'],))
        c.commit()
    assert restarted.process_due()==[]
    assert restarted.get_delivery(later['delivery_id'])['cancel_reason']=='prior_delivery_unresolved'
    assert len(smtp.submissions)==2


def test_period_freeze_cas_cannot_overwrite_another_refresh(flow,monkeypatch):
    db, _, _, _ = flow
    periods = PeriodReportService(db)
    source = periods.generate(CHILD,'weekly')
    with db.connect() as c:
        original = c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',(source['period_report_id'],)).fetchone()[0]
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(ITEM,))
        c.commit()
    build = periods._build_content
    def concurrently_changed(*args,**kwargs):
        content = build(*args,**kwargs)
        with db.connect() as c:
            c.execute("UPDATE period_report SET updated_at='concurrent-writer' WHERE period_report_id=?",(source['period_report_id'],))
            c.commit()
        return content
    monkeypatch.setattr(periods,'_build_content',concurrently_changed)
    with pytest.raises(SnapshotStateError):
        periods.get(source['period_report_id'],persist_refresh=True)
    with db.connect() as c:
        row = c.execute('SELECT content_json,updated_at FROM period_report WHERE period_report_id=?',(source['period_report_id'],)).fetchone()
        assert tuple(row)==(original,'concurrent-writer')


def test_actual_parent_http_queue_unknown_restart_and_corrected_receipt(flow,smtp,tmp_path,monkeypatch):
    db, repo, report, _ = flow
    config = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,data_encryption_key=b'D'*32)
    process = '/v1/parent/report-deliveries/process'
    def unlock(client, initial=False):
        token = client.post('/v1/parent/'+('setup' if initial else 'unlock'),json={'pin':'7391'}).json()['parent_token']
        return {'X-HSC-Parent':token}
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        headers = unlock(client,True)
        live = repo.get_daily_report(report['report_id'])
        snapshot, recipient = prepare((db,repo,live,client.app.state.report_delivery),smtp,'monthly',
            periods=client.app.state.period_reports)
        path = '/v1/parent/report-snapshots/'+snapshot['snapshot_id']
        body = {'recipient_id':recipient['recipient_id']}
        assert client.post(path+'/deliveries',json=body).status_code==401
        queued = client.post(path+'/deliveries',json=body,headers=headers).json()
        smtp.mode = 'unknown'
        response = client.post(process,headers=headers)
        assert response.status_code==200 and response.json()['items'][0]['status']=='unknown'
        assert len(smtp.submissions)==1
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        headers = unlock(client)
        later = (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        monkeypatch.setattr('home_study_agent.report_delivery.utc_now',lambda:later)
        assert client.post(path+'/deliveries',json=body,headers=headers).json()['delivery_id']==queued['delivery_id']
        assert client.post(process,headers=headers).json()=={'items':[]}
        assert len(smtp.submissions)==1
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(ITEM,))
            c.commit()
            before = '\n'.join(c.iterdump())
        projected = client.get(path,headers=headers).json()['correction_status']
        assert projected['source_current'] is False and projected['correction_notification_required']
        with db.connect() as c:
            assert '\n'.join(c.iterdump())==before
        reissue = f"/v1/parent/report-deliveries/{queued['delivery_id']}/unknown/reissue"
        assert client.post(reissue,json={'client_action_id':'stale-http-reissue'},headers=headers).status_code==409
        listing = client.get(f'/v1/parent/children/{CHILD}/report-snapshots',headers=headers)
        assert listing.status_code==200
        previous = client.get(path,headers=headers).json()['correction_status']
        replacement_id = previous['replacements'][-1]['replacement_snapshot_id']
        assert previous['latest_replacement_id']==replacement_id
        assert previous['latest_replacement_availability']=='available'
        assert previous['latest_replacement_status']=='draft'
        other = client.get('/v1/parent/children/child-junior-3/report-snapshots',headers=headers).json()['items']
        assert not any(s['snapshot_id'] in {snapshot['snapshot_id'],replacement_id} for s in other)
        new_path = '/v1/parent/report-snapshots/'+replacement_id
        latest_state = client.get(new_path,headers=headers).json()['correction_status']
        assert latest_state['prior_submissions']==previous['prior_submissions']
        assert latest_state['recipient_ids_requiring_correction']==[recipient['recipient_id']]
        assert client.post(new_path+'/deliveries',json=body,headers=headers).status_code==409
        assert client.post(new_path+'/approve',headers=headers).status_code==200
        assert client.post(new_path+'/deliveries',json=body,headers=headers).status_code==200
        smtp.mode = 'sent'
        assert client.post(process,headers=headers).json()['items'][0]['status']=='sent'
        assert len(smtp.submissions)==2
        final = client.get(path,headers=headers).json()['correction_status']
        assert not final['correction_notification_required']
        assert final['replacement_submissions'][0]['channel_acceptance']=='accepted'
        assert final['replacement_submissions'][0]['is_latest_replacement']
        latest_final = client.get(new_path,headers=headers).json()['correction_status']
        assert latest_final['recipient_ids_requiring_correction']==[]
        assert latest_final['latest_replacement_id']==replacement_id
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        headers = unlock(client)
        assert client.get(path,headers=headers).json()['correction_status']==final
        assert client.get(new_path,headers=headers).json()['correction_status']==latest_final
        assert client.post(process,headers=headers).json()=={'items':[]}
        assert len(smtp.submissions)==2

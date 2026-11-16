"""Visual daily delivery uses only synthetic data and loopback fake SMTP."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from socketserver import StreamRequestHandler, ThreadingTCPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from home_study_agent.daily_report_delivery_state import reserve_local
from home_study_agent.daily_report_observation import prepare
from home_study_agent.notification_channels import ChannelDeliveryError, ChannelSubmissionCancelled, _check_submission, SmtpChannel
from test_daily_report_candidate import synthetic
from test_daily_report_delivery_state import prepared
from test_daily_report_email_verification import setup

DAY = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


class FakeSMTP:
    def __init__(self, db, *, outcome='sent', before_data=None):
        self.db, self.outcome, self.before_data, self.calls = db, outcome, before_data, []

    def send(self, address, subject, text, html, config):
        # Acquiring a separate SQLite writer proves no SQLite write transaction
        # spans network IO; the external-effect guard is deliberately still held.
        with self.db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
        if self.before_data:
            self.before_data()
        _check_submission()
        self.calls.append((address, subject, text, html))
        if self.outcome == 'unknown':
            raise ChannelDeliveryError('SYNTHETIC_UNKNOWN', 'lost response', delivery_unknown=True)
        return SimpleNamespace(provider_message_id='synthetic')


def plan(synthetic, monkeypatch):
    from home_study_agent import daily_report_visual_submission as submission
    db, day, service, _, snapshot = prepared(synthetic)
    reserve_local(service, 'a', day, 'recipient-a', snapshot, now=DAY)
    monkeypatch.setattr(submission, '_utc_now', lambda: DAY)
    fake = FakeSMTP(db)
    service.adapters['email'] = fake
    return submission, db, day, service, snapshot, fake


def test_sent_is_durable_and_never_automatically_repeated(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    result = submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert result['status'] == 'sent'
    assert len(fake.calls) == 1 and fake.calls[0][0] == 'parent@example.test'
    assert '2+2' in fake.calls[0][2]
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'sent'
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert len(fake.calls) == 1


def test_unknown_is_durable_and_never_automatically_repeated(synthetic, monkeypatch):
    submission, db, day, service, snapshot, _ = plan(synthetic, monkeypatch)
    fake = FakeSMTP(db, outcome='unknown'); service.adapters['email'] = fake
    assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == 'unknown'
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'unknown'
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert len(fake.calls) == 1


def test_processing_after_crash_never_automatically_repeats(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    with db.connect() as c:
        c.execute("UPDATE daily_observation_delivery_ledger SET status='processing'"); c.commit()
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert fake.calls == []


def test_wrong_child_cannot_submit_another_child_snapshot(synthetic, monkeypatch):
    submission, _, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    with pytest.raises(ValueError):
        submission.submit(service, 'b', day, 'recipient-a', snapshot)
    assert fake.calls == []


def test_minimal_privacy_omits_ocr_from_both_email_bodies(synthetic, monkeypatch):
    submission, _, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    service.update_preferences('a', '21:00', '07:00', 90, redaction_level='minimal')
    assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == 'sent'
    _, _, text, html = fake.calls[0]
    assert '2+2' not in text and '2+2' not in html
    assert '学习内容细节' in text


def test_second_recipient_requires_its_own_fixed_test_and_receives_own_source(synthetic, monkeypatch):
    from home_study_agent import daily_report_visual_submission as submission
    db, day, service, _, _ = prepared(synthetic)
    second = service.add_recipient('a', 'email', 'second@example.test', 'second', True)['recipient_id']
    with db.connect() as c:
        source = prepare(c, 'a', day, second)
    snapshot = service.freeze_daily_observation('a', day, second, source)['snapshot_id']
    with pytest.raises(ValueError, match='RECIPIENT_EMAIL_TEST_REQUIRED'):
        reserve_local(service, 'a', day, second, snapshot, now=DAY)
    assert service.test_channel('a', 'email', 'second-test', recipient_id=second)['status'] == 'passed'
    reserve_local(service, 'a', day, second, snapshot, now=DAY)
    monkeypatch.setattr(submission, '_utc_now', lambda: DAY)
    fake = FakeSMTP(db); service.adapters['email'] = fake
    assert submission.submit(service, 'a', day, second, snapshot)['status'] == 'sent'
    assert len(fake.calls) == 1 and fake.calls[0][0] == 'second@example.test'
    assert '2+2' in fake.calls[0][2]


@pytest.mark.parametrize('change', ['revoke', 'address', 'credential', 'source', 'deletion', 'restore'])
def test_invalidated_authority_or_source_never_reaches_adapter(synthetic, monkeypatch, change):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    if change == 'revoke':
        service.revoke_recipient('recipient-a')
    elif change == 'credential':
        service._runtime_secrets['email_password'] = 'different-synthetic-password'
    else:
        with db.connect() as c:
            if change == 'address':
                c.execute("UPDATE report_delivery_recipient SET address='other@example.test' WHERE recipient_id='recipient-a'")
            elif change == 'source':
                c.execute("UPDATE capture_artifact SET extracted_text='synthetic changed' WHERE artifact_id='art'")
            elif change == 'deletion':
                c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,client_action_id,created_at,updated_at) VALUES('del','a','reports','child_scope','a','scanning','del-action','x','x')")
            else:
                c.execute("UPDATE daily_observation_delivery_barrier SET blocked=1,reason='RESTORE_HISTORY_UNRECONCILED'")
            c.commit()
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert fake.calls == []


def test_quiet_hours_checked_again_before_data(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    fake.before_data = lambda: monkeypatch.setattr(submission, '_utc_now', lambda: NIGHT)
    result = submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert result['status'] == 'unknown' and fake.calls == []
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'unknown'


@pytest.mark.parametrize('changed', ['source', 'credential', 'consent', 'configuration'])
def test_source_or_credentials_rechecked_after_handshake(synthetic, monkeypatch, changed):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    def mutate():
        if changed == 'credential':
            service._runtime_secrets['email_password'] = 'different-synthetic-password'
        elif changed == 'consent':
            service.revoke_recipient('recipient-a')
        elif changed == 'configuration':
            service.save_channel_config('email', {'host':'127.0.0.2', 'port':25,
                'from_address':'x@example.test', 'allow_external_delivery':True})
        else:
            with db.connect() as c:
                c.execute("UPDATE capture_artifact SET extracted_text='late synthetic change' WHERE artifact_id='art'")
                c.commit()
    fake.before_data = mutate
    result = submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert result['status'] in ('ready', 'unknown')
    assert fake.calls == []


def test_adapter_cannot_forge_pre_data_cancel_after_submission(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    class ForgingAdapter:
        def send(self, *args):
            _check_submission()
            fake.calls.append(('possibly-submitted', '', ''))
            raise ChannelSubmissionCancelled('forged')
    service.adapters['email'] = ForgingAdapter()
    assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == 'unknown'
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'unknown'
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert len(fake.calls) == 1


def test_fake_adapter_cannot_swallow_rejection_and_forge_same_reason(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    class SwallowingAdapter:
        def send(self, *args):
            monkeypatch.setattr(submission, '_utc_now', lambda: NIGHT)
            try:
                _check_submission()
            except ChannelSubmissionCancelled as rejected:
                fake.calls.append(('possibly-submitted', '', ''))
                raise ChannelSubmissionCancelled(rejected.reason)
            pytest.fail('The quiet-hours check should have rejected submission')
    service.adapters['email'] = SwallowingAdapter()
    assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == 'unknown'
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'unknown'
    with pytest.raises(ValueError):
        submission.submit(service, 'a', day, 'recipient-a', snapshot)
    assert len(fake.calls) == 1


def test_two_simultaneous_submissions_send_once(synthetic, monkeypatch):
    submission, db, day, service, snapshot, fake = plan(synthetic, monkeypatch)
    def run(_):
        try: return submission.submit(service, 'a', day, 'recipient-a', snapshot)['status']
        except ValueError: return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results.count('sent') == 1 and len(fake.calls) == 1


@contextmanager
def loopback_smtp(lose_data_reply, before_greeting=None):
    class Server(ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    class Handler(StreamRequestHandler):
        def handle(self):
            if before_greeting:
                before_greeting()
            self.wfile.write(b'220 synthetic.test ESMTP\r\n')
            while True:
                line = self.rfile.readline()
                if not line: return
                command = line.split(None, 1)[0].upper()
                if command in (b'EHLO', b'HELO'):
                    self.wfile.write(b'250 synthetic.test\r\n')
                elif command in (b'MAIL', b'RCPT', b'RSET'):
                    self.wfile.write(b'250 OK\r\n')
                elif command == b'DATA':
                    self.wfile.write(b'354 send data\r\n')
                    body = bytearray()
                    while True:
                        line = self.rfile.readline()
                        if not line: return
                        if line == b'.\r\n': break
                        body.extend(line)
                    self.server.message = bytes(body)
                    self.server.data_count += 1
                    if lose_data_reply: return
                    self.wfile.write(b'250 accepted\r\n')
                elif command == b'QUIT':
                    self.wfile.write(b'221 bye\r\n'); return
                else:
                    self.wfile.write(b'250 OK\r\n')
    server = Server(('127.0.0.1', 0), Handler); server.data_count = 0; server.message = b''
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


@pytest.mark.parametrize('lose_reply', [False, True])
def test_real_smtp_adapter_only_loopback(synthetic, monkeypatch, lose_reply):
    from home_study_agent import daily_report_visual_submission as submission, notification_channels
    with loopback_smtp(lose_reply) as server:
        db, day = synthetic
        _, service, _ = setup(synthetic)
        service.save_channel_config('email', {'host': '127.0.0.1', 'port': server.server_address[1],
            'from_address': 'x@example.test', 'starttls': False, 'allow_external_delivery': True})
        assert service.test_channel('a', 'email', 'synthetic-fixed-test')['status'] == 'passed'
        with db.connect() as c: source = prepare(c, 'a', day, 'recipient-a')
        snapshot = service.freeze_daily_observation('a', day, 'recipient-a', source)['snapshot_id']
        reserve_local(service, 'a', day, 'recipient-a', snapshot, now=DAY)
        monkeypatch.setattr(submission, '_utc_now', lambda: DAY)
        monkeypatch.setattr(notification_channels, '_admit_external_delivery_budget', lambda: None)
        service.adapters['email'] = SmtpChannel()
        assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == ('unknown' if lose_reply else 'sent')
        with pytest.raises(ValueError): submission.submit(service, 'a', day, 'recipient-a', snapshot)
        assert server.data_count == 1
        from email import policy
        from email.parser import BytesParser
        parsed = BytesParser(policy=policy.default).parsebytes(server.message)
        assert parsed['To'] == 'parent@example.test'
        assert '2+2' in parsed.get_body(preferencelist=('plain',)).get_content()


def test_real_smtp_defers_if_quiet_starts_during_handshake(synthetic, monkeypatch):
    from home_study_agent import daily_report_visual_submission as submission, notification_channels
    with loopback_smtp(False, before_greeting=lambda: monkeypatch.setattr(submission, '_utc_now', lambda: NIGHT)) as server:
        db, day = synthetic
        _, service, _ = setup(synthetic)
        service.save_channel_config('email', {'host': '127.0.0.1', 'port': server.server_address[1],
            'from_address': 'x@example.test', 'starttls': False, 'allow_external_delivery': True})
        assert service.test_channel('a', 'email', 'synthetic-fixed-test')['status'] == 'passed'
        with db.connect() as c: source = prepare(c, 'a', day, 'recipient-a')
        snapshot = service.freeze_daily_observation('a', day, 'recipient-a', source)['snapshot_id']
        reserve_local(service, 'a', day, 'recipient-a', snapshot, now=DAY)
        monkeypatch.setattr(submission, '_utc_now', lambda: DAY)
        monkeypatch.setattr(notification_channels, '_admit_external_delivery_budget', lambda: None)
        service.adapters['email'] = SmtpChannel()
        assert submission.submit(service, 'a', day, 'recipient-a', snapshot)['status'] == 'ready'
        assert server.data_count == 0
        with db.connect() as c:
            assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0] == 'ready'

"""Deletion admission and report submission serialize on synthetic databases only."""
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from home_study_agent import privacy_lifecycle
from home_study_agent.database import Database
from home_study_agent.notification_channels import ChannelDeliveryError, _check_submission
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_auto_delivery import verify_and_schedule_reports
from test_report_auto_recipient_verification import setup


def _jobs(db, action):
    with db.connect() as connection:
        return connection.execute('SELECT deletion_id, status FROM deletion_job WHERE client_action_id=?', (action,)).fetchall()


def test_retention_waits_for_external_submission_before_opening_writer(tmp_path, monkeypatch):
    db, *_ = setup(tmp_path)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    monkeypatch.setattr(privacy_lifecycle, 'DELETION_ADMISSION_TIMEOUT_SECONDS', .05)
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with db.external_effect_guard():
            acquired.set()
            assert release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(privacy_lifecycle.DeletionAdmissionBusyError):
            privacy.run_retention()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isinstance(privacy.run_retention(), dict)


@pytest.mark.parametrize('stage', ['handshake', 'before_data'])
@pytest.mark.parametrize('outcome', ['sent', 'unknown'])
def test_real_queue_guard_linearizes_deletion(tmp_path, monkeypatch, stage, outcome):
    db, child, reports, snapshot, delivery, fake, recipient, _ = setup(tmp_path)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    monkeypatch.setattr(privacy_lifecycle, 'DELETION_ADMISSION_TIMEOUT_SECONDS', .1)
    monkeypatch.setattr(delivery, '_in_quiet_hours', lambda _: False)
    assert delivery.test_channel(child, 'email', 'test-recipient', recipient)['status'] == 'passed'
    assert verify_and_schedule_reports(reports, delivery, child, [snapshot])['scheduled'] == 1
    with db.connect() as c:
        delivery_id = c.execute('SELECT delivery_id FROM report_delivery_outbox').fetchone()[0]
    reached, release = threading.Event(), threading.Event()
    results, errors = [], []

    class FakeSMTP:
        calls = 0
        def send(self, *args):
            self.calls += 1
            if stage == 'handshake':
                reached.set()
                assert release.wait(5)
            _check_submission()
            if stage == 'before_data':
                reached.set()
                assert release.wait(5)
            if outcome == 'unknown':
                raise ChannelDeliveryError('FAKE_UNKNOWN', 'synthetic', delivery_unknown=True)
            return SimpleNamespace(provider_message_id='synthetic')

    adapter = FakeSMTP()
    delivery.adapters['email'] = adapter
    def send():
        try:
            results.append(delivery._process_one(delivery_id))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert reached.wait(5), errors
        with pytest.raises(privacy_lifecycle.DeletionAdmissionBusyError):
            privacy.delete_child_scope(child, 'reports', '确认删除', 'same-action')
        assert _jobs(db, 'same-action') == []
    finally:
        release.set()
        thread.join(8)
    assert not thread.is_alive() and not errors
    assert results[0]['status'] == outcome
    if outcome == 'unknown':
        assert delivery._process_one(delivery_id)['status'] == 'unknown'
        assert adapter.calls == 1
    result = privacy.delete_child_scope(child, 'reports', '确认删除', 'same-action')
    repeated = privacy.delete_child_scope(child, 'reports', '确认删除', 'same-action')
    assert result['status'] == 'completed'
    assert repeated['deletion_id'] == result['deletion_id']
    assert len(_jobs(db, 'same-action')) == 1


@pytest.mark.parametrize('kind', ['capture', 'raw', 'knowledge', 'scope'])
def test_cross_process_lock_blocks_every_admission(tmp_path, monkeypatch, kind):
    db, child, *_ = setup(tmp_path)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    monkeypatch.setattr(privacy_lifecycle, 'DELETION_ADMISSION_TIMEOUT_SECONDS', .1)
    script = ("import sys; from pathlib import Path; "
              "from home_study_agent.database import Database; "
              "db=Database(Path(sys.argv[1])); "
              "guard=db.external_effect_guard(); guard.__enter__(); "
              "print('LOCKED',flush=True); sys.stdin.readline(); guard.__exit__(None,None,None)")
    process = subprocess.Popen([sys.executable, '-c', script, str(db.path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'LOCKED'
        operations = {
            'capture': lambda: privacy.delete_capture_artifact('synthetic-missing', '确认删除', 'same-action'),
            'raw': lambda: privacy.delete_capture_artifact_raw_media('synthetic-missing', '确认删除', 'same-action'),
            'knowledge': lambda: privacy.delete_knowledge_source(child, 'synthetic-missing', '确认删除', 'same-action'),
            'scope': lambda: privacy.delete_child_scope(child, 'reports', '确认删除', 'same-action'),
        }
        with pytest.raises(privacy_lifecycle.DeletionAdmissionBusyError):
            operations[kind]()
        assert _jobs(db, 'same-action') == []
    finally:
        process.stdin.write('\n')
        process.stdin.flush()
        process.communicate(timeout=5)
    assert process.returncode == 0
    if kind == 'scope':
        assert operations[kind]()['status'] == 'completed'
        assert len(_jobs(db, 'same-action')) == 1


def test_admitted_deletion_prevents_later_submission(tmp_path, monkeypatch):
    db, child, reports, snapshot, delivery, fake, recipient, _ = setup(tmp_path)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    monkeypatch.setattr(delivery, '_in_quiet_hours', lambda _: False)
    assert delivery.test_channel(child, 'email', 'test-recipient', recipient)['status'] == 'passed'
    assert verify_and_schedule_reports(reports, delivery, child, [snapshot])['scheduled'] == 1
    with db.connect() as c:
        delivery_id = c.execute('SELECT delivery_id FROM report_delivery_outbox').fetchone()[0]
    def interrupted_after_admission(_):
        raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(privacy, '_execute_child_scope_deletion', interrupted_after_admission)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        privacy.delete_child_scope(child, 'reports', '确认删除', 'admitted-action')
    jobs = _jobs(db, 'admitted-action')
    assert len(jobs) == 1 and jobs[0]['status'] != 'completed'
    calls_before = len(fake.messages)
    result = delivery._process_one(delivery_id)
    assert result['status'] == 'scheduled'
    assert len(fake.messages) == calls_before

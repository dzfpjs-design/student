"""A test of yesterday's SMTP configuration cannot open today's setup gate."""

from local_restore_operation_fixture import restore_local
import pytest
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.install_setup import installation_setup_status
from home_study_agent.notification_channels import ChannelReceipt
from test_report_delivery import RecordingEmail

CHILDREN = ('child-junior-3', 'child-primary-4')

@pytest.fixture
def configured(tmp_path):
    db = Database(tmp_path / 'family.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    service = ReportDeliveryService(db, b'T' * 32); service.initialize()
    service.adapters['email'] = RecordingEmail()
    config = {'host': '127.0.0.1', 'port': 12345, 'username': 'synthetic', 'password': 'public-fixture',
              'from_address': 'sender@example.test', 'use_ssl': False, 'starttls': False, 'allow_external_delivery': True}
    service.save_channel_config('email', config)
    for child in CHILDREN:
        service.add_recipient(child, 'email', 'parent@example.test', 'Synthetic parent', True)
        assert service.test_channel(child, 'email', 'before:' + child)['status'] == 'passed'
    return db, repo, service, config

class ConfiguredParent:
    def status(self): return {'configured': True}

def setup(repo, service):
    return installation_setup_status(ConfiguredParent(), repo, service)


@pytest.mark.parametrize('kind', ['local', 'portable'])
def test_restore_requires_new_transport_tests_even_when_smtp_config_is_retained(configured, tmp_path, kind):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, repo, service, _ = configured
    privacy = PrivacyLifecycleService(db, tmp_path, b'T' * 32)
    privacy.initialize()
    passphrase = 'public synthetic restore fixture'
    backup = (privacy.create_backup(prune=False) if kind == 'local'
              else privacy.create_portable_backup(passphrase))
    if kind == 'local':
        restore_local(privacy,backup['backup_id'], '确认恢复')
    else:
        privacy.restore_portable_backup(backup['path'], passphrase, '确认迁移恢复')
    reopened = ReportDeliveryService(Database(db.path), b'T' * 32)
    reopened.initialize()
    reopened.adapters['email'] = RecordingEmail()
    for child in CHILDREN:
        reopened.add_recipient(child, 'email', 'parent@example.test', 'Synthetic parent', True)
    assert setup(StudyRepository(Database(db.path)), reopened)['children_missing_email_test'] == 2
    assert not setup(StudyRepository(Database(db.path)), reopened)['complete']
    # Old successful actions remain truthful historical receipts, never fresh authorization.
    for child in CHILDREN:
        historical = reopened.test_channel(child, 'email', 'before:' + child)
        assert historical['status'] == 'passed'
        assert historical['historical_only'] is True and historical['current_verified'] is False
    assert not setup(StudyRepository(Database(db.path)), reopened)['complete']
    assert reopened.adapters['email'].messages == []
    for child in CHILDREN:
        assert reopened.test_channel(child, 'email', 'restored:' + child)['status'] == 'passed'
    assert len(reopened.adapters['email'].messages) == 2
    assert setup(StudyRepository(Database(db.path)), reopened)['complete']

@pytest.mark.parametrize('change', ['transport', 'secret', 'same_explicit_save'])
def test_new_configuration_requires_actual_tests_for_both_children_and_survives_restart(configured, change):
    db, repo, service, config = configured
    assert setup(repo, service)['complete']
    candidate = dict(config)
    if change == 'transport': candidate['port'] += 1
    if change == 'secret': candidate['password'] = 'different-public-fixture'
    service.save_channel_config('email', candidate)
    assert not setup(repo, service)['complete']
    assert setup(repo, service)['children_missing_email_test'] == 2
    assert len(service.adapters['email'].messages) == 2  # Saving never sends mail.
    reopened = ReportDeliveryService(db, b'T' * 32); reopened.initialize()
    assert setup(repo, reopened)['children_missing_email_test'] == 2
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM report_delivery_channel_test_action WHERE status='passed'").fetchone()[0] == 2
    # Replaying the original receipt is historical, not renewed verification.
    historical = reopened.test_channel(CHILDREN[0], 'email', 'before:' + CHILDREN[0])
    assert historical['status'] == 'passed'
    assert historical['historical_only'] is True and historical['current_verified'] is False
    assert not setup(repo, reopened)['complete']
    reopened.adapters['email'] = RecordingEmail()
    for child in CHILDREN:
        assert reopened.test_channel(child, 'email', 'after:' + child)['status'] == 'passed'
    assert len(reopened.adapters['email'].messages) == 2
    assert setup(repo, reopened)['complete']


def test_old_response_cannot_verify_a_configuration_saved_during_send(configured):
    _, repo, service, config = configured
    class RotateDuringSend:
        def send(self, *args):
            service.save_channel_config('email', {**config, 'port': config['port'] + 1})
            return ChannelReceipt(provider_message_id='synthetic-old-configuration-response')
    service.adapters['email'] = RotateDuringSend()
    assert service.test_channel(CHILDREN[0], 'email', 'old-inflight')['status'] == 'blocked'
    assert setup(repo, service)['children_missing_email_test'] == 2


def test_unknown_history_is_not_erased_or_resent_when_configuration_changes(configured):
    db, _, service, config = configured
    with db.connect() as c:
        c.execute("UPDATE report_delivery_channel_test_action SET status='unknown' WHERE client_action_id=?", ('before:' + CHILDREN[0],)); c.commit()
    service.save_channel_config('email', {**config, 'port': 12346})
    state = service.channel_test_status(CHILDREN[0])[0]
    assert state['status'] == 'unknown'
    assert state['client_action_id'] == 'before:' + CHILDREN[0]
    assert len(service.adapters['email'].messages) == 2


def test_cleared_configuration_cannot_keep_passed_status(configured):
    _, repo, service, _ = configured
    service.clear_channel_config('email')
    assert not setup(repo, service)['complete']
    assert not any(t['status'] == 'passed' for t in service.channel_test_status(CHILDREN[0]))


def test_legacy_pass_older_than_current_configuration_is_not_current_verification(configured):
    db, repo, service, _ = configured
    with db.connect() as c:
        c.execute("UPDATE report_delivery_channel_test SET tested_at='2000-01-01T00:00:00+00:00' WHERE channel='email'"); c.commit()
    assert setup(repo, service)['children_missing_email_test'] == 2


def test_rejected_plaintext_external_config_leaves_verified_transport_and_secret_unchanged(configured):
    db, repo, service, config = configured
    with db.connect() as c:
        before = tuple(c.execute("SELECT credential_id,encrypted_json,updated_at FROM notification_credential WHERE channel='email'").fetchone())
    with pytest.raises(ValueError):
        service.save_channel_config('email', {**config, 'host': 'mail.example.test', 'password': 'rejected-public-secret'})
    with db.connect() as c:
        assert tuple(c.execute("SELECT credential_id,encrypted_json,updated_at FROM notification_credential WHERE channel='email'").fetchone()) == before
    assert service._runtime_secrets['email_password'] == config['password']
    assert setup(repo, service)['complete']

"""Real quarantine, SQLite durability and loopback SMTP; no external recipients."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.report_delivery import ReportDeliveryService, DeliveryStateError
from home_study_agent.install_setup import installation_setup_status
from normal_provider_owner import normal_provider_owner
from test_report_delivery_recovery_journey import smtp


def test_reauthorized_unknown_concurrent_new_actions_send_once(tmp_path, smtp, normal_provider_owner):
    db = Database(tmp_path / 'synthetic-concurrent.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    service = ReportDeliveryService(db, b'T' * 32); service.initialize()
    child = repo.list_children()[0]['child_id']
    service.save_channel_config('email', {'host':'127.0.0.1','port':smtp.server_address[1],
        'from_address':'synthetic@local.test','use_ssl':False,'starttls':False,'allow_external_delivery':True})
    recipient = service.add_recipient(child, 'email', 'parent@local.test', 'Synthetic parent', True)
    smtp.mode = 'unknown'
    assert service.test_channel(child, 'email', 'old-unknown-action')['status'] == 'unknown'
    service.add_recipient(child, 'email', 'parent@local.test', 'Synthetic parent', True)
    smtp.mode = 'sent'; barrier = Barrier(2)
    def attempt(action):
        barrier.wait(timeout=5)
        try:
            return service.reissue_reauthorized_email_test(child, 'old-unknown-action', action, recipient['recipient_id'], 'parent@local.test')['status']
        except DeliveryStateError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['new-action-1', 'new-action-2']))
    assert sorted(results) == ['blocked', 'passed']
    assert len(smtp.submissions) == 2
    restarted = ReportDeliveryService(Database(db.path), b'T' * 32); restarted.initialize()
    assert restarted.channel_test_status(child)[0]['status'] == 'passed'


def test_quarantine_requires_explicit_consent_and_real_tests_after_smtp_reconfiguration(tmp_path, smtp, normal_provider_owner):
    db = Database(tmp_path / 'synthetic-family.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    auth = ParentAuth(db); auth.setup('2468')
    service = ReportDeliveryService(db, b'T' * 32); service.initialize()
    children = [c['child_id'] for c in repo.list_children() if c['status'] == 'active']
    assert len(children) == 2
    config = {'host': '127.0.0.1', 'port': smtp.server_address[1], 'from_address': 'synthetic@local.test',
              'use_ssl': False, 'starttls': False, 'allow_external_delivery': True}
    service.save_channel_config('email', config)
    for child in children:
        service.add_recipient(child, 'email', 'synthetic-parent@local.test', 'Synthetic parent', True)
        assert service.test_channel(child, 'email', 'pre-restore:' + child)['status'] == 'passed'
    assert installation_setup_status(auth, repo, service)['complete']
    assert len(smtp.submissions) == 2
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        service.quarantine_after_restore(connection, 'synthetic-restore-quarantine')
        connection.commit()
    quarantined = installation_setup_status(auth, repo, service)
    assert quarantined == {'schema':'hsc.installation-setup.v1','existing_family':False,'complete':False,'parent_pin_configured':True,
        'smtp_configured':True,'smtp_secret_check_required':True,'active_child_count':2,
        'children_missing_email_recipient':2,'children_missing_email_test':2,'next_step':'email_recipients'}
    print('QUARANTINED_SETUP_STATUS=' + json.dumps(quarantined, sort_keys=True))
    assert len(smtp.submissions) == 2
    service.save_channel_config('email', {**config, 'from_address':'new-synthetic@local.test'})
    assert len(smtp.submissions) == 2
    assert installation_setup_status(auth, repo, service)['children_missing_email_recipient'] == 2
    for child in children:
        service.add_recipient(child, 'email', 'synthetic-parent@local.test', 'Synthetic parent', True)
    assert not installation_setup_status(auth, repo, service)['complete']
    assert installation_setup_status(auth, repo, service)['children_missing_email_test'] == 2
    for child in children:
        assert service.test_channel(child, 'email', 'post-restore:' + child)['status'] == 'passed'
    assert len(smtp.submissions) == 4
    restarted = ReportDeliveryService(Database(db.path), b'T' * 32); restarted.initialize()
    durable = installation_setup_status(ParentAuth(Database(db.path)), StudyRepository(Database(db.path)), restarted)
    assert durable['complete'] and durable['next_step'] == 'complete'
    assert durable['children_missing_email_recipient'] == durable['children_missing_email_test'] == 0
    assert len(smtp.submissions) == 4


def test_unknown_and_restored_processing_tests_require_explicit_original_reissue_and_survive_restart(tmp_path, smtp, normal_provider_owner):
    db = Database(tmp_path / 'synthetic-uncertain.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    auth = ParentAuth(db); auth.setup('2468')
    service = ReportDeliveryService(db, b'T' * 32); service.initialize()
    children = [c['child_id'] for c in repo.list_children() if c['status'] == 'active']
    service.save_channel_config('email', {'host':'127.0.0.1','port':smtp.server_address[1],
        'from_address':'synthetic@local.test','use_ssl':False,'starttls':False,'allow_external_delivery':True})
    for child in children:
        service.add_recipient(child, 'email', 'parent@local.test', 'Synthetic parent', True)
        assert service.test_channel(child, 'email', 'original-pass:' + child)['status'] == 'passed'
    smtp.mode = 'unknown'
    unknown = service.test_channel(children[0], 'email', 'original-lost-ack')
    assert unknown['status'] == 'unknown'
    with db.connect() as connection:
        action = connection.execute("SELECT status,message,tested_at FROM report_delivery_channel_test_action WHERE client_action_id='original-lost-ack'").fetchone()
        summary = connection.execute("SELECT status,message,tested_at FROM report_delivery_channel_test WHERE child_id=? AND channel='email'", (children[0],)).fetchone()
        assert tuple(action) == ('unknown', unknown['message'], unknown['tested_at'])
        assert tuple(summary) == ('blocked', unknown['message'], unknown['tested_at'])
        assert connection.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0] == 0
    assert len(smtp.submissions) == 3
    # Original reservation creates a real processing action without a submission.
    assert service._reserve_channel_test_action(children[1], 'email', 'original-processing') is None
    assert service.channel_test_status(children[1])[0]['status'] == 'processing'
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        service.quarantine_after_restore(connection, 'synthetic-uncertain-restore')
        connection.commit()
    for child in children:
        service.add_recipient(child, 'email', 'parent@local.test', 'Synthetic parent', True)
        assert service.channel_test_status(child)[0]['status'] == 'unknown'
    service.keep_unknown_channel_test(children[0], 'email', 'original-lost-ack')
    restarted = ReportDeliveryService(Database(db.path), b'T' * 32); restarted.initialize()
    assert restarted.channel_test_status(children[0])[0]['kept_without_resend']
    assert not installation_setup_status(ParentAuth(Database(db.path)), StudyRepository(Database(db.path)), restarted)['complete']
    assert len(smtp.submissions) == 3
    smtp.mode = 'sent'
    previous = ['original-lost-ack', 'original-processing']
    for index, child in enumerate(children):
        action = 'explicit-replacement:' + child
        recipient = restarted.list_recipients(child)[0]
        with pytest.raises(DeliveryStateError):
            restarted.reissue_unknown_channel_test(child, 'email', previous[index], action)
        assert len(smtp.submissions) == 3 + index
        with pytest.raises(DeliveryStateError):
            restarted.reissue_reauthorized_email_test(child, previous[index], action, recipient['recipient_id'], 'wrong@local.test')
        with pytest.raises(DeliveryStateError):
            restarted.reissue_reauthorized_email_test(child, previous[index], action, 'different-recipient', recipient['address'])
        assert len(smtp.submissions) == 3 + index
        assert restarted.reissue_reauthorized_email_test(child, previous[index], action, recipient['recipient_id'], recipient['address'])['status'] == 'passed'
        count = len(smtp.submissions)
        assert restarted.reissue_reauthorized_email_test(child, previous[index], action, recipient['recipient_id'], recipient['address'])['status'] == 'passed'
        assert len(smtp.submissions) == count  # Exact replay cannot submit again.
    assert len(smtp.submissions) == 5
    final = ReportDeliveryService(Database(db.path), b'T' * 32); final.initialize()
    assert installation_setup_status(ParentAuth(Database(db.path)), StudyRepository(Database(db.path)), final)['complete']
    with db.connect() as connection:
        states = dict(connection.execute('SELECT client_action_id,status FROM report_delivery_channel_test_action'))
        old = connection.execute("SELECT message,replacement_action_id FROM report_delivery_channel_test_action WHERE client_action_id='original-lost-ack'").fetchone()
        assert '旧测试结果未知' in old['message'] and old['replacement_action_id'] == 'explicit-replacement:' + children[0]
        assert connection.execute("SELECT count(*) FROM audit_event WHERE action='report_delivery_channel_test_reissued'").fetchone()[0] == 2
    assert states['original-lost-ack'] == states['original-processing'] == 'superseded'


@pytest.mark.parametrize('invalid', ['processing', 'unknown_acknowledged', 'superseded', 'success', None, True])
def test_channel_test_completion_rejects_nonterminal_or_unknown_status_before_writes(tmp_path, invalid):
    db = Database(tmp_path / 'synthetic-closed-status.db'); db.initialize()
    service = ReportDeliveryService(db, b'T' * 32); service.initialize()
    with db.connect() as connection:
        before = [list(connection.execute('SELECT * FROM ' + table)) for table in ('report_delivery_channel_test','report_delivery_channel_test_action','audit_event')]
    with pytest.raises(ValueError, match='不支持的邮件测试结果状态'):
        service._complete_channel_test_action('synthetic-child', 'email', 'not-an-admitted-request', invalid, 'Synthetic')
    with db.connect() as connection:
        after = [list(connection.execute('SELECT * FROM ' + table)) for table in ('report_delivery_channel_test','report_delivery_channel_test_action','audit_event')]
    assert before == after

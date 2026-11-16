from local_restore_operation_fixture import restore_local
"""A restored queue is quarantined before any live consumer can see it."""
import hashlib

import pytest
from normal_provider_owner import normal_provider_owner

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_delivery import ReportDeliveryService
from test_report_delivery_recovery_journey import flow, smtp, prepare


@pytest.mark.parametrize('kind', ['local', 'portable'])
@pytest.mark.parametrize('transport', ['sent', 'unknown'])
def test_first_published_restore_is_quarantined_and_source_is_unchanged(
        flow, smtp, tmp_path, monkeypatch, kind, transport, normal_provider_owner):
    db, _, _, delivery = flow
    snapshot, recipient = prepare(flow, smtp, 'daily')
    queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'])
    privacy = PrivacyLifecycleService(db, tmp_path, b'D' * 32)
    privacy.initialize()
    password = 'synthetic restore boundary passphrase'
    backup = (privacy.create_backup(prune=False) if kind == 'local'
              else privacy.create_portable_backup(password))
    from pathlib import Path
    source = Path(backup['path'])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    smtp.mode = transport
    assert delivery.process_due()[0]['status'] == transport
    assert len(smtp.submissions) == 1
    observations = []
    if kind == 'local':
        from home_study_agent import restore_publication as publication
        publish = publication.publish_prepared_database

        def inspect_first_live_generation(owner):
            result = publish(owner)
            if owner.path == db.path:
                with db.connect() as c:
                    observations.append(tuple(c.execute('''SELECT o.status,r.consent_status,s.status
                    FROM report_delivery_outbox o JOIN report_delivery_recipient r USING(recipient_id)
                    JOIN parent_report_snapshot s USING(snapshot_id) WHERE o.delivery_id=?''',
                        (queued['delivery_id'],)).fetchone()))
                assert delivery.process_due() == []
            return result

        monkeypatch.setattr(publication, 'publish_prepared_database', inspect_first_live_generation)
    else:
        initialize = Database.initialize

        def inspect_first_live_generation(instance):
            if instance.path == db.path:
                with instance.connect() as c:
                    observations.append(tuple(c.execute('''SELECT o.status,r.consent_status,s.status
                    FROM report_delivery_outbox o JOIN report_delivery_recipient r USING(recipient_id)
                    JOIN parent_report_snapshot s USING(snapshot_id) WHERE o.delivery_id=?''',
                        (queued['delivery_id'],)).fetchone()))
                assert delivery.process_due() == []
            return initialize(instance)

        monkeypatch.setattr(Database, 'initialize', inspect_first_live_generation)
    result = (restore_local(privacy,backup['backup_id'], '确认恢复') if kind == 'local'
              else privacy.restore_portable_backup(backup['path'], password, '确认迁移恢复'))
    assert result['status'] == 'restored'
    assert observations == [('unknown', 'pending', 'draft')]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    restarted = ReportDeliveryService(Database(db.path), b'D' * 32)
    restarted.initialize()
    assert restarted.process_due() == []
    assert len(smtp.submissions) == 1
    restored_delivery = restarted.get_delivery(queued['delivery_id'])
    assert restored_delivery['status'] == 'unknown'
    assert restored_delivery['last_error_code'] == 'RESTORE_DELIVERY_UNCERTAIN'
    assert restored_delivery['attempts'] == []

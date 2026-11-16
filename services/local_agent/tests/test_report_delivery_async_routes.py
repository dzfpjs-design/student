"""The child API remains responsive while a parent mutation waits for SMTP."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database, ExternalEffectBusyError
from home_study_agent.report_delivery import ReportDeliveryService


@pytest.mark.parametrize(('method', 'verb', 'path', 'body', 'expected'), [
    ('save_channel_config', 'PUT', '/v1/parent/report-delivery/channels/email', {'config': {}}, 200),
    ('clear_channel_config', 'DELETE', '/v1/parent/report-delivery/channels/email', None, 200),
    ('add_recipient', 'POST', '/v1/parent/report-delivery/recipients', {'child_id': 'child-junior-3', 'channel': 'email', 'address': 'parent@example.test', 'label': 'synthetic', 'consented': True}, 200),
    ('revoke_recipient', 'DELETE', '/v1/parent/report-delivery/recipients/missing', None, 404),
    ('update_preferences', 'PUT', '/v1/parent/children/child-junior-3/report-delivery/preferences', {}, 200),
    ('test_channel', 'POST', '/v1/parent/children/child-junior-3/report-delivery/channels/email/test', {'client_action_id': 'synthetic-action-1'}, 200),
    ('reissue_unknown_channel_test', 'POST', '/v1/parent/children/child-junior-3/report-delivery/channels/email/test/reissue', {'client_action_id': 'synthetic-action-2', 'previous_client_action_id': 'synthetic-action-1'}, 409),
    ('reissue_reauthorized_email_test', 'POST', '/v1/parent/children/child-junior-3/report-delivery/channels/email/test/reissue-reauthorized', {'client_action_id': 'synthetic-action-2', 'previous_client_action_id': 'synthetic-action-1', 'recipient_id': 'synthetic-recipient', 'confirmed_address': 'parent@example.test'}, 409),
    ('process_due', 'POST', '/v1/parent/report-deliveries/process', None, 200),
])
def test_parent_write_waiting_for_effect_guard_does_not_block_child_or_health(tmp_path, monkeypatch, method, verb, path, body, expected):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'synthetic.db', data_encryption_key=b'x'*32, maintenance_enabled=False)
    entered, holding, release = Event(), Event(), Event()
    original = getattr(ReportDeliveryService, method)

    def mark_entry(self, *args, **kwargs):
        entered.set()
        if method == 'process_due':
            # With no scheduled rows this operation returns immediately. Model
            # a sender waiting for the same guard without creating real mail.
            with self.database.external_effect_guard(timeout=5):
                pass
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ReportDeliveryService, method, mark_entry)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        db = Database(settings.database_path)

        def hold_guard():
            with db.external_effect_guard(timeout=5):
                holding.set()
                assert release.wait(10)

        holder = Thread(target=hold_guard, daemon=True)
        holder.start()
        assert holding.wait(5)
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            pending = pool.submit(client.request, verb, path, headers=headers, json=body)
            assert entered.wait(5)
            health = pool.submit(client.get, '/v1/health')
            child = pool.submit(client.get, '/v1/children/child-junior-3/mistakes')
            try:
                assert health.result(timeout=2).status_code == 200
                assert child.result(timeout=2).status_code == 200
            except TimeoutError:
                pytest.fail('the waiting parent operation blocked the ASGI event loop')
            assert not pending.done()
        finally:
            release.set()
            holder.join(5)
            pool.shutdown(wait=True)
        assert pending.result().status_code == expected


def test_threaded_parent_mutation_preserves_retryable_busy_response(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'synthetic.db', data_encryption_key=b'x'*32, maintenance_enabled=False)

    def busy(*_args, **_kwargs):
        raise ExternalEffectBusyError('synthetic lock contention')

    monkeypatch.setattr(ReportDeliveryService, 'save_channel_config', busy)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        response = client.put('/v1/parent/report-delivery/channels/email', headers={'X-HSC-Parent': token}, json={'config': {}})
        assert response.status_code == 409
        assert response.json()['code'] == 'RESTORE_EXTERNAL_EFFECT_BUSY'
        assert response.json()['retryable'] is True
        assert response.headers['cache-control'] == 'no-store'


def test_reauthorized_email_reissue_requires_parent_auth(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'synthetic.db', data_encryption_key=b'x'*32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = client.post('/v1/parent/children/child-junior-3/report-delivery/channels/email/test/reissue-reauthorized',
            json={'client_action_id':'synthetic-action-2','previous_client_action_id':'synthetic-action-1',
                  'recipient_id':'synthetic-recipient','confirmed_address':'parent@example.test'})
        assert response.status_code in (401, 403)

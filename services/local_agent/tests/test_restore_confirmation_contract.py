"""Recovery errors must name the confirmation required by that same operation."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
from dataclasses import replace
import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_companion_activity_host import NoCloud
from test_native_shared_source_erasure import family
from test_native_shared_source_host import settings_for, parent_headers


@pytest.mark.parametrize('portable', [False, True])
def test_restore_http_error_names_its_actual_confirmation(family, portable):
    db = family[0]
    settings = settings_for(db, encrypted=True)
    settings = replace(settings,native_launch_attestation_key=KEY if not portable else None)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        headers = parent_headers(client, settings)
        body = ({'path': str(db.path.parent / 'absent.hscbackup'),
                 'passphrase': 'isolated confirmation contract'} if portable
                else {'backup_id': 'absent-backup'})
        body['confirmation'] = 'wrong'
        body['operation'] = restore_operation(client, headers=headers)
        route = '/v1/parent/backups/' + ('portable/restore' if portable else 'restore')
        response = (client.post(route, headers=headers, json=body) if portable else
            signed_local_restore_post(client, headers=headers, json=body))
        assert response.status_code == 422, response.text
        expected = '确认迁移恢复' if portable else '确认恢复'
        assert response.json() == {'code': 'RESTORE_CONFIRMATION_REQUIRED',
                                   'message': f'请输入“{expected}”。'}

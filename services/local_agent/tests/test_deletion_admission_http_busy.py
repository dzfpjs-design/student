"""The real parent route distinguishes a busy admission from an old receipt."""
from home_study_agent.database import Database
from test_app import client_for
from test_privacy_operation_recovery_http import auth, identity, read


def test_parent_delete_busy_does_not_reserve_and_same_request_can_retry(tmp_path):
    with client_for(tmp_path) as client:
        headers = auth(client, True)
        operation = identity(client, headers)
        payload = {'scope': 'reports', 'confirmation': '确认删除',
                   'client_action_id': operation['request_id'], 'operation': operation}
        database = Database(tmp_path / 'test.db')
        with database.external_effect_guard():
            response = client.post('/v1/parent/children/child-primary-4/delete',
                                   headers=headers, json=payload)
        assert response.status_code == 409, response.text
        assert response.json()['code'] == 'DELETION_ADMISSION_BUSY'
        assert response.json()['retryable'] is True
        with database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job WHERE client_action_id=?',
                             (operation['request_id'],)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM privacy_operation_intent WHERE request_id=?',
                             (operation['request_id'],)).fetchone()[0] == 0
        response = client.post('/v1/parent/children/child-primary-4/delete',
                               headers=headers, json=payload)
        assert response.status_code == 200, response.text
        receipt = read(client, headers, operation)
        assert receipt['cleanup_allowed'] is True
        with database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job WHERE client_action_id=?',
                             (operation['request_id'],)).fetchone()[0] == 1


def test_parent_delete_interleaved_async_task_cannot_inherit_owner(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from starlette.requests import Request
    from home_study_agent import restore_publication as publication

    with client_for(tmp_path) as client:
        headers = auth(client, True)
        first, second = identity(client, headers), identity(client, headers)
        original_json = Request.json
        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()
            held = False
            async def pausing_json(request):
                nonlocal held
                if (request.url.path.endswith('/delete') and not held
                    and publication._gate(publication._path(tmp_path / 'test.db')).owner is not None):
                    held = True
                    entered.set()
                    await asyncio.wait_for(release.wait(), 5)
                return await original_json(request)
            monkeypatch.setattr(Request, 'json', pausing_json)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://testserver') as http:
                def payload(operation):
                    return {'scope': 'reports', 'confirmation': '确认删除',
                            'client_action_id': operation['request_id'], 'operation': operation}
                path = '/v1/parent/children/child-primary-4/delete'
                task = asyncio.create_task(http.post(path, headers=headers, json=payload(first)))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    competing = await http.post(path, headers=headers, json=payload(second))
                    assert competing.status_code != 200, competing.text
                finally:
                    release.set()
                original = await asyncio.wait_for(task, 15)
                assert original.status_code == 200, original.text
        client.portal.call(exercise)
        with Database(tmp_path / 'test.db').connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job WHERE client_action_id=?',
                             (first['request_id'],)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM deletion_job WHERE client_action_id=?',
                             (second['request_id'],)).fetchone()[0] == 0

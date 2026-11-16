"""Publication holds postpone maintenance, without obtaining write authority."""
import asyncio
import logging
from threading import Event

import pytest

from home_study_agent import restore_publication as publication
from home_study_agent.visual_retention import VisualRetentionService
from home_study_agent.visual_capture_producer import VisualCaptureProducer
from test_visual_capture_maintenance import Schedule, producer
from test_visual_capture_producer import local_client, parent_headers


@pytest.mark.parametrize('code', [
    'RESTORE_PUBLICATION_BUSY', 'RESTORE_PUBLICATION_RECOVERY_REQUIRED',
    'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED',
])
@pytest.mark.parametrize('operation', ['cleanup', 'sweep'])
def test_known_publication_hold_recovers_same_coroutine_with_bounded_retry(monkeypatch, caplog, code, operation):
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    calls = {'cleanup': [], 'sweep': []}

    def work(name):
        def run():
            calls[name].append(schedule.time)
            if name == operation and len(calls[name]) < 3:
                raise publication.RestorePublicationError(code)
        return run

    service.retention.cleanup = work('cleanup')
    service.sweep = work('sweep')
    schedule.stop = lambda: len(calls[operation]) >= 4
    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        asyncio.run(service.maintain())
    assert calls[operation][:3] == [0, 1, 3]
    assert calls[operation][3] - calls[operation][2] == (30 if operation == 'cleanup' else 1)
    assert len(cleared) == 1
    assert code in caplog.text
    assert 'visual_capture_maintenance_recovered' in caplog.text


def test_unknown_publication_error_is_not_swallowed(monkeypatch):
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    error = publication.RestorePublicationError('UNKNOWN_PROGRAMMING_FAILURE')

    def cleanup():
        raise error

    service.retention.cleanup = cleanup
    with pytest.raises(publication.RestorePublicationError) as caught:
        asyncio.run(service.maintain())
    assert caught.value is error
    assert schedule.waits == [] and len(cleared) == 1


def test_actual_app_original_publication_owner_blocks_then_same_maintenance_task_resumes(tmp_path, monkeypatch, caplog):
    original = VisualCaptureProducer.sweep
    entered, run_original, blocked, recovered = (Event() for _ in range(4))
    errors = []
    calls = []

    def sweep(self):
        calls.append(self)
        if len(calls) == 1:
            entered.set()
            assert run_original.wait(8)
        try:
            result = original(self)
        except publication.RestorePublicationError as error:
            errors.append(error)
            blocked.set()
            raise
        recovered.set()
        return result

    async def current_maintenance():
        matches = [task for task in asyncio.all_tasks()
                   if task.get_coro().__qualname__ == 'VisualCaptureProducer.maintain']
        assert len(matches) == 1
        return matches[0]

    monkeypatch.setattr(VisualCaptureProducer, 'sweep', sweep)
    try:
        with caplog.at_level(logging.INFO):
            with local_client(tmp_path) as client:
                assert entered.wait(8)
                original_task = client.portal.call(current_maintenance)
                database = client.app.state.privacy_lifecycle.database
                with publication.publication_owner(database, timeout=2):
                    run_original.set()
                    assert blocked.wait(8)
                    assert str(errors[0]) == 'RESTORE_PUBLICATION_BUSY'
                    assert not original_task.done()
                assert recovered.wait(8)
                assert client.portal.call(current_maintenance) is original_task
                assert not original_task.done()
                headers = parent_headers(client)
                response = client.get('/v1/parent/children/child-junior-3/visual-retention', headers=headers)
                assert response.status_code == 200, response.text
                assert calls[0] is calls[1] is client.app.state.visual_capture
            assert original_task.cancelled()
    finally:
        run_original.set()
    assert 'visual_capture_maintenance_recovered' in caplog.text
    assert str(tmp_path) not in caplog.text

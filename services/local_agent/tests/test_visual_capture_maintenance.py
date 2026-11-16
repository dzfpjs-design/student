"""Real coroutine/to_thread failures; deterministic monotonic time, no app import."""
import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from home_study_agent import visual_capture_producer as module


@pytest.fixture(autouse=True)
def exact_source_copy():
    expected = Path(__file__).resolve().parents[1] / 'src/home_study_agent/visual_capture_producer.py'
    assert Path(module.__file__).resolve() == expected


class Schedule:
    def __init__(self):
        self.time = 0.0
        self.waits = []
        self.stop = lambda: False

    async def sleep(self, delay):
        assert delay > 0, 'maintenance must not busy loop'
        self.waits.append(delay)
        assert len(self.waits) < 300
        if self.stop():
            raise asyncio.CancelledError()
        self.time += delay
        await asyncio.sleep(0)


def producer(monkeypatch, schedule):
    cleared = []
    host = SimpleNamespace(clear_prepared=lambda **kw: cleared.append(kw))
    result = module.VisualCaptureProducer(None, host, None, detector=object(),
        clock=lambda: schedule.time)
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(
        to_thread=asyncio.to_thread, sleep=schedule.sleep))
    return result, cleared


@pytest.mark.parametrize('operation', ['cleanup', 'sweep'])
@pytest.mark.parametrize('failure', [OSError('private/path'), sqlite3.OperationalError('database is locked')])
def test_single_failure_recovers_without_ending_maintenance(monkeypatch, caplog, operation, failure):
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    calls = {'cleanup': [], 'sweep': []}

    def job(name):
        def run():
            calls[name].append(schedule.time)
            if name == operation and len(calls[name]) == 1:
                raise failure
        return run

    service.retention.cleanup = job('cleanup')
    service.sweep = job('sweep')
    schedule.stop = lambda: len(calls[operation]) >= 3 and len(calls['sweep']) >= 3
    with caplog.at_level(logging.INFO):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(service.maintain())
    assert len(calls[operation]) >= 3
    assert calls[operation][1] > calls[operation][0]
    assert calls[operation][2] - calls[operation][1] == (30 if operation == 'cleanup' else 1)
    assert len(cleared) == 1
    assert 'visual_capture_maintenance_retry' in caplog.text
    assert 'visual_capture_maintenance_recovered' in caplog.text
    assert 'private/path' not in caplog.text


def test_repeated_cleanup_failure_is_bounded_and_sweep_continues(monkeypatch, caplog):
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    cleanup_times, sweep_times = [], []

    def cleanup():
        cleanup_times.append(schedule.time)
        raise OSError('private/path')

    service.retention.cleanup = cleanup
    service.sweep = lambda: sweep_times.append(schedule.time)
    schedule.stop = lambda: schedule.time >= 100
    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(service.maintain())
    gaps = [b-a for a,b in zip(cleanup_times, cleanup_times[1:])]
    assert gaps == [1, 2, 4, 8, 16, 30, 30]
    assert len(sweep_times) >= 95
    assert len(cleared) == 1
    assert 'private/path' not in caplog.text


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('no such table: private_table'),
                                  sqlite3.IntegrityError('private constraint'), RuntimeError('bug')])
def test_nonrecoverable_database_or_programming_error_propagates(monkeypatch, failure):
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)

    def cleanup():
        raise failure

    service.retention.cleanup = cleanup
    service.sweep = lambda: None
    with pytest.raises(type(failure)) as caught:
        asyncio.run(service.maintain())
    assert caught.value is failure
    assert schedule.waits == []
    assert len(cleared) == 1


def test_cancel_while_worker_is_running_exits_without_retry(monkeypatch):
    import threading
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def cleanup():
        calls.append('cleanup')
        entered.set()
        assert release.wait(5)

    service.retention.cleanup = cleanup
    service.sweep = lambda: calls.append('sweep')

    async def run():
        task = asyncio.create_task(service.maintain())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(run())
    assert calls == ['cleanup']
    assert len(cleared) == 1


def test_real_sqlite_lock_release_recovers_same_maintenance_task(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'synthetic.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE maintenance_probe(value INTEGER)')
    blocker = sqlite3.connect(path)
    blocker.execute('BEGIN IMMEDIATE')
    schedule = Schedule()
    service, cleared = producer(monkeypatch, schedule)
    attempts, swept = [], []

    def cleanup():
        attempts.append(schedule.time)
        with sqlite3.connect(path, timeout=0) as c:
            c.execute('INSERT INTO maintenance_probe VALUES (1)')

    async def release_on_backoff(delay):
        blocker.rollback()
        await schedule.sleep(delay)

    module.asyncio.sleep = release_on_backoff
    service.retention.cleanup = cleanup
    service.sweep = lambda: swept.append(schedule.time)
    schedule.stop = lambda: len(attempts) >= 3
    try:
        with caplog.at_level(logging.INFO):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(service.maintain())
    finally:
        blocker.close()
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT count(*) FROM maintenance_probe').fetchone()[0] == 2
    assert attempts == [0, 1, 31]
    assert len(swept) >= 30
    assert len(cleared) == 1
    assert 'visual_capture_maintenance_recovered' in caplog.text
    assert str(path) not in caplog.text


def test_actual_app_lifespan_survives_cleanup_failure_and_closes_cleanly(tmp_path, monkeypatch, caplog):
    from threading import Event
    from home_study_agent.visual_retention import VisualRetentionService
    from test_visual_capture_producer import local_client, parent_headers

    original = VisualRetentionService.cleanup
    recovered = Event()
    calls = []

    def cleanup(self, **kwargs):
        calls.append(self)
        if len(calls) == 1:
            raise OSError('private local path')
        result = original(self, **kwargs)
        recovered.set()
        return result

    monkeypatch.setattr(VisualRetentionService, 'cleanup', cleanup)
    with caplog.at_level(logging.INFO):
        with local_client(tmp_path) as client:
            assert recovered.wait(10)
            h = parent_headers(client)
            response = client.get('/v1/parent/children/child-junior-3/visual-retention', headers=h)
            assert response.status_code == 200, response.text
            assert response.json()['policy']['status'] == 'disabled'
            assert calls[0] is calls[1] is client.app.state.visual_retention
    assert 'visual_capture_maintenance_recovered' in caplog.text
    assert 'private local path' not in caplog.text

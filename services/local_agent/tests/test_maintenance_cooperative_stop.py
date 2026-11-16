"""Synthetic lifecycle/SQLite regression tests; no provider or device calls."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib
from pathlib import Path
import socket
import threading
import time

import pytest

from home_study_agent.database import Database
from home_study_agent.maintenance_work import (
    MaintenanceDrainUnconfirmed, MaintenanceStopped, MaintenanceWork,
)
from home_study_agent.restore_publication import RestorePublicationError


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, (str, bytes)):  # asyncio's own Unix socket.
            return original(sock, address)
        raise AssertionError("SYNTHETIC_TEST_NETWORK_FORBIDDEN")

    def no_dns(*args, **kwargs):
        raise AssertionError("SYNTHETIC_TEST_DNS_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


async def wait_event(event):
    deadline = time.monotonic() + 15
    while not event.is_set():
        assert time.monotonic() < deadline, "SYNTHETIC_EVENT_TIMEOUT"
        await asyncio.sleep(.002)


@pytest.mark.parametrize("job_error", [False, True])
def test_original_thread_drains_after_repeated_cancel(job_error):
    async def scenario():
        work = MaintenanceWork()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def original_job():
            entered.set()
            try:
                assert release.wait(10)
                if job_error:
                    raise RestorePublicationError("RESTORE_PUBLICATION_CLOSE_UNCONFIRMED")
                return "completed"
            finally:
                finished.set()

        task = asyncio.create_task(work.run(original_job))
        try:
            await wait_event(entered)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set() and work._active
            release.set()
            expected = RestorePublicationError if job_error else asyncio.CancelledError
            with pytest.raises(expected):
                await task
            assert finished.is_set() and not work._active and work.is_stopping()
        finally:
            release.set()
            if not task.done():
                with pytest.raises((asyncio.CancelledError, RestorePublicationError)):
                    await task

    asyncio.run(scenario())


def test_stopped_queued_work_never_enters_business():
    from concurrent.futures import ThreadPoolExecutor

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered, release = threading.Event(), threading.Event()
        work, calls = MaintenanceWork(), []

        def blocker():
            entered.set()
            assert release.wait(10)

        first = loop.run_in_executor(None, blocker)
        try:
            await wait_event(entered)
            task = asyncio.create_task(work.run(lambda: calls.append(True)))
            await asyncio.sleep(.01)
            work.request_stop()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await first
            assert calls == [] and not work._active
            with pytest.raises(MaintenanceStopped):
                await work.run(lambda: calls.append(True))
            assert calls == []
        finally:
            release.set()
            await first

    asyncio.run(scenario())


def test_directly_cancelled_original_task_retained_as_unconfirmed():
    async def scenario():
        work = MaintenanceWork()
        entered, release = threading.Event(), threading.Event()

        def original_job():
            entered.set()
            assert release.wait(10)

        task = asyncio.create_task(work.run(original_job))
        try:
            await wait_event(entered)
            original = next(iter(work._active))
            original.cancel()
            with pytest.raises(MaintenanceDrainUnconfirmed):
                await task
            assert original in work._active and original.cancelled()
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.fixture
def supply(tmp_path):
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
    from home_study_agent.variant_supply import VariantSupplyService

    root = tmp_path.resolve() / "private"
    root.mkdir(mode=0o700)
    db = Database(root / "synthetic.db", runtime_mode="trial")
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    privacy = PrivacyLifecycleService(db, root, b"x" * 32)
    privacy.initialize()

    class NoProvider:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("PROVIDER_FORBIDDEN")

    return db, privacy, SpecializedDiagnosticSupplyService(db, NoProvider()), VariantSupplyService(db, NoProvider())


def test_supply_stop_preserves_original_job_transaction_and_resume(supply):
    db, _, specialized, variants = supply
    first = specialized.enqueue_local("diag-j3-discriminant-1", "child-junior-3", "synthetic-first-job")
    jobs = [first] + specialized.enqueue_local_gaps(
        "child-primary-4", "competition_math", "synthetic-remaining-jobs", limit=5,
    )["jobs"]
    assert len(jobs) >= 2
    stop = threading.Event()
    original = specialized._store

    def store_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    specialized._store = store_then_stop
    partial = specialized.process_local_pending(50, should_stop=stop.is_set)
    assert partial["processed"] == 1 and partial["stopped"] is True
    assert not db._original_connections
    with db.connect() as connection:
        rows = connection.execute("SELECT status,attempt_count FROM specialized_supply_job WHERE generation_mode='local'").fetchall()
        pending = sum(row[0] == "pending" for row in rows)
        assert pending == len(jobs) - 1
        assert all(row[1] == 0 for row in rows if row[0] == "pending")
        assert all(row[0] != "processing" for row in rows)
    specialized._store = original
    resumed = specialized.process_local_pending(50)
    assert resumed["processed"] == pending and "stopped" not in resumed
    assert variants.process_local_pending(20, should_stop=lambda: True)["processed"] == 0
    assert not db._original_connections


def test_started_backup_finishes_manifest_despite_repeated_cancel(supply):
    db, privacy, _, _ = supply

    async def scenario():
        work = MaintenanceWork()
        entered, release = threading.Event(), threading.Event()
        original = privacy._backup_checksum
        saved = {}

        def checksum(path):
            value = original(path)
            entered.set()
            assert release.wait(10)
            return value

        privacy._backup_checksum = checksum
        task = asyncio.create_task(work.run(lambda: saved.update(privacy.create_backup(prune=False))))
        try:
            await wait_event(entered)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and work._active
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert saved["integrity_status"] == "ok" and not work._active
            with db.connect() as connection:
                assert [row[0] for row in connection.execute("SELECT integrity_status FROM backup_manifest")] == ["ok"]
            assert not db._original_connections
        finally:
            release.set()
            privacy._backup_checksum = original

    asyncio.run(scenario())


def test_default_supply_has_original_response_and_no_learning_writes(supply):
    from home_study_agent.local_supply_delivery import refresh_local_question_supply
    db, _, specialized, variants = supply
    reports = [refresh_local_question_supply(db, specialized, variants) for _ in range(2)]
    assert sum(row["qualified"] for row in reports) == 10
    assert all("stopped" not in row for row in reports)
    with db.connect() as connection:
        assert dict(connection.execute("SELECT track,count(*) FROM specialized_supply_candidate WHERE status='promoted' GROUP BY track")) == {
            "junior_exam": 3, "competition_math": 5, "pet": 2,
        }
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mastery_state").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["none", "visual", "all"])
def test_actual_lifespan_drains_before_any_early_error_returns(tmp_path, monkeypatch, failure):
    from home_study_agent import money_egress
    from home_study_agent.config import Settings
    from home_study_agent.document_learning import DocumentLearningService
    from home_study_agent.visual_capture_producer import VisualCaptureProducer

    monkeypatch.setattr(money_egress, "SLOT", tmp_path.resolve() / "nonexistent-synthetic-money-slot")
    application = importlib.import_module("home_study_agent.app")
    entered, release, finished, visual_started = (threading.Event() for _ in range(4))
    close_calls = []

    class VisualStopFailed(RuntimeError):
        pass

    class GatewayCloseFailed(RuntimeError):
        pass

    class VoiceCloseFailed(RuntimeError):
        pass

    def held_refresh(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(10)
            if failure == "all":
                raise RestorePublicationError("RESTORE_PUBLICATION_CLOSE_UNCONFIRMED")
            return {"synthetic_completed": True}
        finally:
            finished.set()

    async def visual_maintain(self):
        visual_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if failure != "none":
                raise VisualStopFailed("SYNTHETIC_VISUAL_STOP_FAILED")
            raise

    async def idle_document(self, limit=1):
        await asyncio.Event().wait()

    monkeypatch.setattr(application, "refresh_local_question_supply", held_refresh)
    monkeypatch.setattr(VisualCaptureProducer, "maintain", visual_maintain)
    monkeypatch.setattr(DocumentLearningService, "process_pending", idle_document)
    root = tmp_path.resolve() / "app-private"
    root.mkdir(mode=0o700)
    app = application.create_app(Settings(
        data_dir=root, database_path=root / "app.db", runtime_mode="trial",
        session_token="synthetic-maintenance-lifespan", data_encryption_key=b"x" * 32,
        maintenance_enabled=True,
    ))
    gateway_close, voice_close = app.state.model_gateway.close, app.state.voice_synthesis.close

    async def close_gateway():
        await gateway_close()
        close_calls.append("gateway")
        if failure == "all":
            raise GatewayCloseFailed("SYNTHETIC_GATEWAY_CLOSE_FAILED")

    async def close_voice():
        await voice_close()
        close_calls.append("voice")
        if failure == "all":
            raise VoiceCloseFailed("SYNTHETIC_VOICE_CLOSE_FAILED")

    monkeypatch.setattr(app.state.model_gateway, "close", close_gateway)
    monkeypatch.setattr(app.state.voice_synthesis, "close", close_voice)

    async def scenario():
        observed = None
        try:
            async with app.router.lifespan_context(app):
                await wait_event(entered)
                await wait_event(visual_started)
                # Original owned thread stays held across shutdown entry.
                asyncio.get_running_loop().call_later(.05, release.set)
        except Exception as error:
            observed = error
        try:
            assert finished.is_set(), "LIFESPAN_RETURNED_BEFORE_ORIGINAL_THREAD_DRAINED"
            assert close_calls == ["gateway", "voice"], "ORIGINAL_CLIENT_CLOSE_SKIPPED_OR_REPEATED"
            chain, cursor, seen = [], observed, set()
            while cursor is not None and id(cursor) not in seen and len(chain) < 16:
                seen.add(id(cursor)); chain.append(type(cursor)); cursor = cursor.__context__
            if failure == "none":
                assert observed is None
            elif failure == "visual":
                assert type(observed) is VisualStopFailed
            else:
                assert type(observed) is VoiceCloseFailed
                assert all(kind in chain for kind in [VisualStopFailed, RestorePublicationError, GatewayCloseFailed])
            assert money_egress.current_owner().closed is True
            with Database(root / "app.db", runtime_mode="trial").connect() as connection:
                assert connection.execute("SELECT clean_shutdown FROM runtime_state").fetchone()[0] == (1 if failure == "none" else 0)
        finally:
            release.set()  # Original worker is allowed to finish even when testing the RED source.
            await wait_event(finished)

    asyncio.run(scenario())


@pytest.mark.parametrize("document_error", [False, True])
def test_document_thread_drains_despite_earlier_visual_shutdown_error(tmp_path, monkeypatch, document_error):
    from home_study_agent import money_egress
    from home_study_agent.config import Settings
    from home_study_agent.document_learning import DocumentLearningService
    from home_study_agent.visual_capture_producer import VisualCaptureProducer

    monkeypatch.setattr(money_egress, "SLOT", tmp_path.resolve() / "nonexistent-document-money-slot")
    application = importlib.import_module("home_study_agent.app")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    close_calls, document_finished_before_close = [], []

    class VisualStopFailed(RuntimeError):
        pass

    async def visual_maintain(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise VisualStopFailed("SYNTHETIC_VISUAL_STOP_FAILED")

    async def synthetic_completed_analysis(self, limit=1):
        # A completed public synthetic analysis triggers the original memory lane
        # immediately; no provider/analyze_revision or clock replacement.
        return [{"synthetic_completed": True}]

    monkeypatch.setattr(DocumentLearningService, "process_pending", synthetic_completed_analysis)
    monkeypatch.setattr(VisualCaptureProducer, "maintain", visual_maintain)
    root = tmp_path.resolve() / "document-app-private"
    root.mkdir(mode=0o700)
    app = application.create_app(Settings(
        data_dir=root, database_path=root / "app.db", runtime_mode="trial",
        session_token="synthetic-document-lifespan", data_encryption_key=b"x" * 32,
        maintenance_enabled=True,
    ))
    original_memory = app.state.companion_memory.process_pending

    def held_document_memory(limit):
        if limit != 10:  # The original document loop dispatches exactly 10.
            return original_memory(limit)
        entered.set()
        try:
            assert release.wait(10)
            value = original_memory(limit)  # Real original SQL work and finalizers.
            if document_error:
                raise RestorePublicationError("RESTORE_PUBLICATION_CLOSE_UNCONFIRMED")
            return value
        finally:
            finished.set()

    monkeypatch.setattr(app.state.companion_memory, "process_pending", held_document_memory)
    original_gateway, original_voice = app.state.model_gateway.close, app.state.voice_synthesis.close

    async def close_gateway():
        document_finished_before_close.append(finished.is_set())
        await original_gateway()
        close_calls.append("gateway")

    async def close_voice():
        await original_voice()
        close_calls.append("voice")

    monkeypatch.setattr(app.state.model_gateway, "close", close_gateway)
    monkeypatch.setattr(app.state.voice_synthesis, "close", close_voice)

    async def scenario():
        try:
            with pytest.raises(RestorePublicationError if document_error else VisualStopFailed) as caught:
                async with app.router.lifespan_context(app):
                    await wait_event(entered)
                    asyncio.get_running_loop().call_later(1.0, release.set)
            if document_error:
                cursor, seen, chain = caught.value, set(), []
                while cursor is not None and id(cursor) not in seen and len(chain) < 16:
                    seen.add(id(cursor)); chain.append(type(cursor)); cursor = cursor.__context__
                assert VisualStopFailed in chain
            assert finished.is_set(), "LIFESPAN_RETURNED_BEFORE_ORIGINAL_DOCUMENT_THREAD_DRAINED"
            assert close_calls == ["gateway", "voice"]
            assert document_finished_before_close == [True], "CLIENT_CLOSE_STARTED_BEFORE_ORIGINAL_DOCUMENT_DRAIN"
            assert money_egress.current_owner().closed is True
            assert not app.state.companion_memory.database._original_connections
            with Database(root / "app.db", runtime_mode="trial").connect() as connection:
                assert connection.execute("SELECT clean_shutdown FROM runtime_state").fetchone()[0] == 0
        finally:
            release.set()
            if entered.is_set():
                await wait_event(finished)

    asyncio.run(scenario())

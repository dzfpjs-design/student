"""CPU queue transactions release real SQLite resources without waiting for GC."""
from concurrent.futures import ThreadPoolExecutor
import gc
import sqlite3
from types import SimpleNamespace

import pytest

from home_study_agent.local_ocr import resource_budget
from home_study_agent.local_ocr.resource_budget import CpuResourceQueue, ResourceSnapshot


@pytest.fixture
def delayed_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
        gc.collect()


@pytest.fixture
def real_connections(monkeypatch, delayed_gc):
    # Retain the actual objects: a destructor or convenient GC cannot hide a leak.
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True

    def connect(*args, **kwargs):
        assert kwargs == {}  # Preserve SQLite's timeout/isolation/thread defaults.
        connection = sqlite3.connect(*args, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(resource_budget, "sqlite3", SimpleNamespace(
        connect=connect, Row=sqlite3.Row, Connection=sqlite3.Connection,
    ))
    try:
        yield connections
    finally:
        for connection in connections:
            connection.close()


def assert_closed_and_released(connections, path):
    assert connections and all(c.closed for c in connections), "queue leaked SQLite connections"
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    # Some SQLite builds retain empty sidecars after the final close. A fresh
    # connection must still acquire the exclusive journal-mode lock immediately;
    # another retained WAL connection makes this real SQLite operation fail.
    probe = sqlite3.connect(path, timeout=0)
    try:
        assert probe.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        probe.execute("BEGIN EXCLUSIVE")
        probe.rollback()
    finally:
        probe.close()


def test_repeated_queue_calls_release_real_connections_without_gc(tmp_path, real_connections):
    path = tmp_path / "cpu-resource-queue.db"
    queue = CpuResourceQueue(path)
    capacity = ResourceSnapshot(cpu_percent=0, memory_mb=0)
    for index in range(12):
        task_id = f"public-ocr-{index}"
        queue.enqueue("ocr", {"synthetic": True}, task_id)
        assert queue.behavior_fps_cap() == .25
        assert queue.next(ResourceSnapshot(cpu_percent=90, memory_mb=0)) is None
        assert queue.claim(task_id, capacity)["task_id"] == task_id
        queue.finish(task_id, "cancelled")
        assert queue.retry(task_id)["status"] == "queued"
        assert queue.next(capacity)["task_id"] == task_id
        assert queue.recover_orphaned(max_running_seconds=0) == 1
        assert queue.claim(task_id, capacity)["task_id"] == task_id
        queue.finish(task_id, "cancelled")
        assert queue.behavior_fps_cap() == .5
    assert len(real_connections) > 100
    assert_closed_and_released(real_connections, path)
    # A new queue sees the same committed results after every connection closed.
    reopened = CpuResourceQueue(path)
    assert reopened.next(capacity) is None
    assert_closed_and_released(real_connections, path)


def test_transaction_defaults_commit_and_exception_rollback_also_close(tmp_path, real_connections):
    path = tmp_path / "cpu-resource-queue.db"
    queue = CpuResourceQueue(path)
    queue.enqueue("behavior", task_id="public-behavior")
    with queue._connect() as db:
        assert db.row_factory is sqlite3.Row
        assert db.isolation_level == ""
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        db.execute("UPDATE cpu_task SET reason='committed' WHERE task_id='public-behavior'")
    with pytest.raises(RuntimeError, match="public rollback"):
        with queue._connect() as db:
            db.execute("UPDATE cpu_task SET reason='must not persist' WHERE task_id='public-behavior'")
            raise RuntimeError("public rollback")
    with queue._connect() as db:
        assert db.execute("SELECT reason FROM cpu_task").fetchone()[0] == "committed"
    capacity = ResourceSnapshot(cpu_percent=0, memory_mb=0)
    assert queue.claim("missing-public-task", capacity) is None
    with pytest.raises(KeyError, match="Unknown task"):
        queue.finish("missing-public-task")
    with pytest.raises(KeyError, match="Unknown task"):
        queue.retry("missing-public-task")
    assert_closed_and_released(real_connections, path)


def test_failed_transaction_commit_rolls_back_and_closes(tmp_path, real_connections):
    path = tmp_path / "cpu-resource-queue.db"
    queue = CpuResourceQueue(path)
    with pytest.raises(sqlite3.IntegrityError):
        with queue._connect() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE public_parent(id INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE public_child(parent_id INTEGER REFERENCES public_parent(id) DEFERRABLE INITIALLY DEFERRED)")
            db.execute("INSERT INTO public_child VALUES (123)")
    with queue._connect() as db:
        assert db.execute("SELECT count(*) FROM public_child").fetchone()[0] == 0
    assert_closed_and_released(real_connections, path)


def test_concurrent_cpu_claims_keep_original_resource_limits(tmp_path, delayed_gc):
    queue = CpuResourceQueue(tmp_path / "cpu-resource-queue.db")
    task_ids = [f"public-ocr-{i}" for i in range(8)]
    for task_id in task_ids:
        queue.enqueue("ocr", task_id=task_id)
    capacity = ResourceSnapshot(cpu_percent=0, memory_mb=0)
    with ThreadPoolExecutor(max_workers=8) as workers:
        claimed = list(workers.map(lambda task_id: queue.claim(task_id, capacity), task_ids))
    running = [task for task in claimed if task is not None]
    assert len(running) == 2
    assert sum(task["budget"]["threads"] for task in running) == 6
    assert sum(task["budget"]["memory_mb"] for task in running) == 3600
    for task in running:
        queue.finish(task["task_id"])
    assert queue.next(capacity) is not None

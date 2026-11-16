"""Synthetic test instrumentation. No source text, SQL text or trust secrets emitted."""
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import traceback
from time import perf_counter
from types import MethodType


class ExecutionMeasurement:
    def __init__(self, monkeypatch, database, service_type, *,
                 additional_databases=(), additional_services=()):
        self.db = database
        self.enabled = False
        self.sql = Counter()
        self.reads = []
        self.read_callers = []
        self.transactions = []
        self.lock_samples = []
        self.stop = threading.Event()
        self.local = threading.local()
        read = service_type._read
        initialize = service_type.__init__

        databases = []
        for owner in (database, *additional_databases):
            assert Path(owner.path) == Path(database.path)
            if any(owner is current for current in databases):
                continue
            databases.append(owner)
            connect = owner.connect
            def instrument_connect(owner, connect):
                @contextmanager
                def measured_connect(*args, **kwargs):
                    with connect(*args, **kwargs) as c:
                        began = [None]
                        def trace(sql):
                            if not self.enabled:
                                return
                            op = sql.lstrip().split(None, 1)[0].upper()
                            self.sql[op] += 1
                            if op == 'BEGIN' and 'IMMEDIATE' in sql.upper():
                                began[0] = perf_counter()
                            elif op in {'COMMIT', 'ROLLBACK'} and began[0] is not None:
                                self.transactions.append((perf_counter()-began[0])*1000)
                                began[0] = None
                        c.set_trace_callback(trace)
                        try:
                            yield c
                        finally:
                            c.set_trace_callback(None)
                monkeypatch.setattr(owner, 'connect', measured_connect)
            instrument_connect(owner, connect)
        def measured_read(service, c, row, now, **kwargs):
            active = self.enabled and not getattr(self.local, 'reading', False)
            start = perf_counter()
            if active:
                self.local.reading = True
            try:
                return read(service, c, row, now, **kwargs)
            finally:
                if active:
                    self.reads.append((perf_counter()-start)*1000)
                    self.read_callers.append([f.name for f in traceback.extract_stack(limit=9)[:-1]])
                    self.local.reading = False
        def instrument_service(service):
            assert type(service) is service_type
            monkeypatch.setattr(service, '_read', MethodType(measured_read, service))
        def measured_initialize(service, *args, **kwargs):
            initialize(service, *args, **kwargs)
            instrument_service(service)
        # Leave the original class-level factory/read code identities intact.
        # HTTP owns another explicit Database instance; services created with
        # database=None also perform real borrowed reads and must be measured.
        monkeypatch.setattr(service_type, '__init__', measured_initialize)
        for service in additional_services:
            instrument_service(service)

    def _probe(self):
        with sqlite3.connect(str(self.db.path), timeout=0) as c:
            while not self.stop.is_set():
                busy = False
                try:
                    c.execute('BEGIN IMMEDIATE')
                    c.rollback()
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                        raise
                    busy = True
                self.lock_samples.append((perf_counter(), busy))
                self.stop.wait(.01)

    def run(self, operation, *, lock_probe=False):
        self.sql.clear()
        self.reads.clear()
        self.read_callers.clear()
        self.transactions.clear()
        self.lock_samples.clear()
        self.stop.clear()
        self.enabled = True
        worker = threading.Thread(target=self._probe) if lock_probe else None
        if worker:
            worker.start()
        start = perf_counter()
        try:
            value = operation()
        finally:
            elapsed = (perf_counter()-start)*1000
            self.enabled = False
            self.stop.set()
            if worker:
                worker.join(timeout=3)
                assert not worker.is_alive()
        spans, began = [], None
        for at, busy in self.lock_samples:
            if busy and began is None:
                began = at
            if not busy and began is not None:
                spans.append((at-began)*1000)
                began = None
        if began is not None:
            spans.append((self.lock_samples[-1][0]-began)*1000)
        return value, dict(elapsed_ms=elapsed, sql_count=sum(self.sql.values()), sql_operations=dict(self.sql),
            calibration_read_ms=self.reads[:], begin_to_commit_ms=self.transactions[:],
            calibration_read_callers=self.read_callers[:],
            observed_write_busy_ms=spans, lock_probe=lock_probe, probe_interval_ms=10)


def checkpoint(database, root, name, trust_path, metadata):
    target = Path(root)/name
    target.mkdir(parents=True, exist_ok=False)
    with database.connect() as c, sqlite3.connect(str(target/'checkpoint.db')) as backup:
        c.backup(backup)
    # The trust file contains public signer keys only. The synthetic signing key
    # remains process-local and is never part of checkpoints.
    shutil.copy2(trust_path, target/'public-trust.json')
    (target/'context.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    return target

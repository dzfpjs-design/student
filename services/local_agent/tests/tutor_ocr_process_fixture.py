"""Two Tutor races use separate real App processes, with fixed event projection.

This is a synthetic test fixture, not a provider or native authorization bridge.
Only actual child Gateway messages/events are mirrored. ModelRequest.pre_dispatch
is never transmitted or reconstructed as an operational permission.
"""
from contextlib import contextmanager
from dataclasses import fields
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

import httpx
from home_study_agent.model_gateway import ModelRequest

_MAX_LINE = 65536
_RETAINED = []  # Unknown original process/pipe ownership is never discarded.


class _GatewayProjection:
    def __init__(self, gateway):
        self.gateway = gateway
        self.workers = []
        self.lock = threading.RLock()
        self.release = getattr(gateway, 'release', None)
        if self.release is not None:
            original_set = self.release.set
            def release_all():
                original_set()
                with self.lock:
                    for worker in tuple(self.workers):
                        worker.command('release')
            self.release.set = release_all

    def attach(self, worker):
        with self.lock:
            self.workers.append(worker)
            if self.release is not None and self.release.is_set():
                worker.command('release')

    def detach(self, worker):
        with self.lock:
            self.workers.remove(worker)

    def event(self, kind, value):
        with self.lock:
            if kind == 'request':
                expected = {field.name for field in fields(ModelRequest)}
                assert type(value) is dict and set(value) == expected
                # Dataclass fields exclude the InitVar pre_dispatch callback.
                assert 'pre_dispatch' not in value
                value = dict(value)
                for name in ('messages', 'evidence_refs'):
                    value[name] = tuple(value[name])
                self.gateway.messages.append(ModelRequest(**value))
            else:
                assert kind in {'started', 'returned'}
                getattr(self.gateway, kind).set()


class _OriginalWorker:
    def __init__(self, root, gateway, projection):
        self.root, self.projection = root, projection
        self.nonce = str(uuid4())
        self.errors = []
        self.ready = threading.Event()
        self.closed = None
        self.port = None
        self.seen_requests = 0
        self.write_lock = threading.Lock()
        self.forced = False
        self.waited = False
        helper = Path(__file__).parent / 'fixtures' / 'tutor_ocr_http_process.py'
        service = helper.parents[2]
        mode = 'blocking' if hasattr(gateway, 'release') else 'replay'
        model = getattr(gateway, 'model', 'fake-tutor-v1')
        assert (mode, model) in {('blocking', 'blocking-test'), ('blocking', 'old-attempt'),
                                ('blocking', 'confirmed-retry'), ('replay', 'fake-tutor-v1')}
        env = dict(os.environ)
        for key in list(env):
            upper = key.upper()
            if upper.startswith('HSC_') or upper in {'KEY','TOKEN','PASSWORD','SECRET','CREDENTIALS','WEBHOOK',
                'PYTHONHOME','PYTHONSTARTUP','PYTEST_ADDOPTS','PYTEST_PLUGINS'} or upper.endswith(
                    ('_KEY','_TOKEN','_PASSWORD','_SECRET','_CREDENTIALS','_WEBHOOK')):
                env.pop(key)
        env.update(HSC_DATA_DIR=str(root), HSC_DATABASE_PATH=str(root/'test.db'),
                   PYTHONPATH=os.pathsep.join((str(service/'src'), str(service/'tests'))),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        self.stderr_path = root / ('.tutor-process-' + self.nonce + '.stderr')
        self.stderr = self.stderr_path.open('xb')
        try:
            self.process = subprocess.Popen([sys.executable, '-B', str(helper), str(root), mode, model, self.nonce],
                cwd=service, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        except BaseException:
            self.stderr.close()
            raise
        self.reader = threading.Thread(target=self._read, name='owned-tutor-event-reader', daemon=True)
        self.reader.start()
        projection.attach(self)

    def command(self, command):
        assert command in {'release','stop'}
        with self.write_lock:
            if self.process.poll() is None:
                self.process.stdin.write((command+'\n').encode('ascii'))
                self.process.stdin.flush()

    def _read(self):
        total = events = 0
        try:
            while True:
                raw = self.process.stdout.readline(_MAX_LINE+1)
                if not raw:
                    break
                total += len(raw); events += 1
                assert len(raw) <= _MAX_LINE and raw.endswith(b'\n') and total <= 524288 and events <= 32
                value = json.loads(raw)
                assert type(value) is dict and value.pop('nonce') == self.nonce
                pid = value.pop('pid')
                # The original Popen object, not a reopened PID, binds the IPC.
                assert type(pid) is int and pid == self.process.pid
                kind = value.pop('kind')
                if kind == 'ready':
                    assert set(value) == {'port'} and self.port is None
                    assert type(value['port']) is int and 0 < value['port'] < 65536
                    self.port = value['port']; self.ready.set()
                elif kind == 'request':
                    assert set(value) == {'index','request'} and type(value['index']) is int and value['index'] == self.seen_requests
                    self.projection.event('request', value['request']); self.seen_requests += 1
                elif kind in {'started','returned'}:
                    assert value == {} and self.seen_requests > 0
                    self.projection.event(kind, None)
                elif kind == 'closed':
                    assert self.closed is None
                    assert set(value) == {'owner_closed','normal_only','http_drained','http_force_exit','cleanup_release'}
                    assert all(type(x) is bool for x in value.values())
                    self.closed = value
                elif kind == 'error':
                    assert set(value) == {'code'} and value['code'] == 'TUTOR_CHILD_FAILED'
                    self.errors.append(value['code'])
                else:
                    raise AssertionError('Unexpected Tutor IPC event')
        except BaseException as error:
            self.errors.append(error)
        finally:
            self.ready.set()  # Wakes startup only; it never fabricates a ready port.

    def finish(self):
        code = None
        try:
            try:
                self.command('stop')
            except (BrokenPipeError, OSError) as error:
                self.errors.append(error)
            try:
                code = self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.forced = True
                self.process.terminate()
                try:
                    code = self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    code = self.process.wait(timeout=5)
            self.waited = True
            self.reader.join(2)
            assert not self.reader.is_alive(), 'Original Tutor IPC did not close'
            assert code == 0 and not self.forced and not self.errors, 'Original Tutor process failed or needed force'
            assert self.closed == dict(owner_closed=True, normal_only=True, http_drained=True,
                                       http_force_exit=False, cleanup_release=False)
        except BaseException:
            if not self.waited or self.reader.is_alive():
                _RETAINED.append(self)
            raise
        finally:
            self.projection.detach(self)
            if self.waited and not self.reader.is_alive():
                self.process.stdin.close(); self.process.stdout.close(); self.stderr.close()
            receipt = dict(schema=1, original_Popen=True, native_exit=code,
                wait_returned=self.waited, original_pipes_EOF=not self.reader.is_alive(),
                forced=self.forced, error_count=len(self.errors), actual_worker_requests=self.seen_requests,
                closed=self.closed, retained_unknown=self in _RETAINED)
            with (self.root/('.tutor-process-'+self.nonce+'.receipt.json')).open('x',encoding='utf-8') as f:
                json.dump(receipt,f,ensure_ascii=False)


@contextmanager
def tutor_process_client(tmp_path, gateway):
    root = Path(tmp_path).resolve(strict=True)
    assert root.name.startswith('test_') and any(p.name.startswith('pytest-') for p in root.parents)
    projection = getattr(gateway, '_tutor_process_projection', None)
    if projection is None:
        projection = _GatewayProjection(gateway)
        gateway._tutor_process_projection = projection
    worker = _OriginalWorker(root, gateway, projection)
    try:
        assert worker.ready.wait(30) and worker.port is not None and not worker.errors, 'Tutor child not ready'
        with httpx.Client(base_url=f'http://127.0.0.1:{worker.port}', trust_env=False, timeout=30) as client:
            yield client
    finally:
        worker.finish()

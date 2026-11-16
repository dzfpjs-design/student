"""Two fixed synthetic restart lanes; each replacement owns a real OS process.

Only original synthetic provider callbacks, ASR output and the test learning
clock cross private pipes. No App/owner/permission object is imitated.
"""
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
import asyncio
import base64
import inspect
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx

LIMIT = 2 * 1024 * 1024
LANES = {'primary_oral', 'supply_gap'}
_OWNED_ENDPOINTS = {}
_ENDPOINT_LOCK = threading.RLock()


def owned_synthetic_connection(address, timeout=None, source_address=None):
    """Only the exact READY original child endpoint may replace the old no-socket seam."""
    with _ENDPOINT_LOCK:
        original = _OWNED_ENDPOINTS.get(address) if type(address) is tuple else None
        assert original is not None and original.poll() is None, 'Real network forbidden in C03 isolated probe'
        assert address[0] == '127.0.0.1' and type(address[1]) is int
        assert source_address is None and type(timeout) in (int, float) and 0 < timeout <= 30
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            connection.connect(address)
            assert original.poll() is None, 'original synthetic child ended during connect'
            return connection
        except BaseException:
            connection.close()
            raise


def _line(stream, value, lock):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    assert len(data) <= LIMIT, 'synthetic process metadata bound'
    with lock:
        stream.write(data)
        stream.flush()


def _read(stream):
    data = stream.readline(LIMIT + 1)
    if not data:
        return None
    assert len(data) <= LIMIT and data.endswith(b'\n'), 'synthetic process frame bound'
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, 'duplicate process metadata key'
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=unique)
    assert type(value) is dict
    return value


def _clock_value():
    from home_study_agent import repository
    clock = repository.datetime
    assert getattr(clock, '_hsc_test_clock', False), 'original learning clock required'
    return clock.current.isoformat()


def _settings_wire(settings):
    allowed = {
        'model_api_key': {'not-a-real-key', 'isolated-no-real-key'},
        'session_token': {None, 'isolated-c03'},
        'data_encryption_key': {None, b'8' * 32},
    }
    result = {}
    for field in fields(settings):
        value = getattr(settings, field.name)
        if field.name.endswith(('_key', '_token')) and value is not None:
            assert field.name in allowed and value in allowed[field.name], 'public fixture setting required'
        if isinstance(value, Path):
            value = {'path': str(value)}
        elif isinstance(value, bytes):
            value = {'bytes': value.hex()}
        result[field.name] = value
    return result


@contextmanager
def unclean_app(case, *, lane, headers=None):
    """Keep all original App contexts alive; host only the new App elsewhere."""
    assert lane in LANES
    settings = case['settings']
    old_owner = case['client'].app.state.money_owner
    assert old_owner.closed is False
    root = settings.data_dir.resolve(strict=True)
    assert any(p.startswith('pytest-') or p.startswith('pytest-of-') for p in root.parts)
    assert settings.database_path.resolve(strict=True).is_relative_to(root)
    expected_transport = 'test_open_response_spoken_runtime_http.py' if lane == 'primary_oral' else 'test_supply_gap_resume_http.py'
    assert callable(case['gateway_transport']) and case['gateway_transport'].__code__.co_filename.endswith(expected_transport)
    wire_settings = _settings_wire(settings)
    _clock_value()
    record = root / ('unclean-process-' + uuid.uuid4().hex)
    record.mkdir(mode=0o700)
    nonce = uuid.uuid4().hex
    env = dict(os.environ)
    for name in list(env):
        if name.upper().startswith('HSC_') or name.upper().endswith(('_KEY', '_TOKEN', '_PASSWORD', '_SECRET', '_CREDENTIALS', '_WEBHOOK')):
            env.pop(name)
    service = Path(__file__).resolve().parents[1]
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               PYTHONPATH=str(service / 'src'), HSC_DATA_DIR=str(record / 'unused-import-root'),
               HSC_DATABASE_PATH=str(record / 'unused-import-root' / 'unused.db'))
    ready = threading.Event()
    closed = threading.Event()
    errors = []
    observations = []
    original_provider_exceptions = []
    state = {}
    send_lock = threading.RLock()
    parent_sequence = [0]

    def send(value):
        with send_lock:
            parent_sequence[0] += 1
            _line(original.stdin, {'nonce': nonce, 'parent_pid': os.getpid(),
                                  'seq': parent_sequence[0], **value}, send_lock)

    from home_study_agent.local_media_ai import LocalMediaAI

    def answer(message):
        operation = message['op']
        if operation == 'clock':
            return _clock_value()
        if operation == 'clock_advance':
            from home_study_agent import repository
            value = datetime.fromisoformat(message['value'])
            clock = repository.datetime
            assert getattr(clock, '_hsc_test_clock', False) and value.tzinfo is not None
            assert abs((value - clock.current).total_seconds()) <= 5
            clock.current = max(clock.current, value)
            return clock.current.isoformat()
        if operation == 'asr':
            recognize = LocalMediaAI._recognize_wav
            assert recognize.__code__.co_filename.endswith('test_voice_runtime_binding.py')
            # The original capture fixture's lambda ignores its arguments.
            value = recognize()
            assert type(value) is tuple and len(value) == 4 and value[2:] == ('fixture', False)
            assert type(value[0]) is str and len(value[0]) <= 8192 and value[1] == 1000
            return list(value)
        if operation == 'primary_plan':
            assert lane == 'primary_oral'
            command = case['command_state']
            assert command['control'] in {'task.start', 'task.resume', 'task.complete'}
            assert type(command['quote']) is str and len(command['quote']) <= 8192
            assert set(command['start_selector']) == {'subject', 'task_kind', 'source_type', 'target_quote'}
            return {key: command[key] for key in ('control', 'start_selector', 'quote')}
        assert operation == 'http'
        value = message['value']
        assert value['method'] == 'POST'
        assert value['url'] == settings.model_base_url.rstrip('/') + '/chat/completions'
        request = httpx.Request(value['method'], value['url'], headers=value['headers'],
                                content=base64.b64decode(value['body'], validate=True))
        response = case['gateway_transport'](request)
        if inspect.isawaitable(response):
            response = asyncio.run(response)
        assert type(response) is httpx.Response
        return {'status': response.status_code, 'headers': list(response.headers.multi_items()),
                'body': base64.b64encode(response.content).decode()}

    with (record / 'stderr.log').open('xb') as stderr:
        original = subprocess.Popen([sys.executable, '-B', '-u', str(Path(__file__).resolve()), '--child'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                    cwd=service, env=env)

        def receive():
            child_sequence = 0
            try:
                while True:
                    message = _read(original.stdout)
                    if message is None:
                        break
                    assert message.get('nonce') == nonce and message.get('original_pid') == original.pid
                    assert type(message.get('seq')) is int and message['seq'] == child_sequence + 1
                    child_sequence += 1
                    kind = message['kind']
                    if kind == 'rpc':
                        try:
                            result = {'ok': True, 'value': answer(message)}
                        except json.JSONDecodeError as error:
                            assert message['op'] == 'http'
                            original_provider_exceptions.append({'type': 'JSONDecodeError', 'op': 'http'})
                            result = {'ok': False, 'exception': 'JSONDecodeError',
                                      'msg': error.msg, 'doc': error.doc, 'pos': error.pos}
                        except BaseException as error:
                            errors.append(type(error).__name__ + ':' + str(error)[:512])
                            result = {'ok': False, 'error': type(error).__name__}
                        send({'kind': 'reply', 'id': message['id'], **result})
                    elif kind == 'ready':
                        assert message['database_path'] == str(settings.database_path.resolve())
                        assert message['original_pid'] == original.pid
                        assert message['source_package_root'] == str((service / 'src').resolve())
                        assert message['actual_owner_active'] is False and message['actual_owner_closed'] is False
                        assert 'port' not in state, 'duplicate child ready'
                        state.update(message)
                        ready.set()
                    elif kind == 'closed':
                        assert 'closure' not in state, 'duplicate child close'
                        state['closure'] = message
                        closed.set()
                    else:
                        raise AssertionError('unknown synthetic process event')
                    observations.append({'kind': kind, 'op': message.get('op')})
            except BaseException as error:
                errors.append(type(error).__name__ + ':' + str(error)[:512])
            finally:
                ready.set()

        reader = threading.Thread(target=receive, name='original-synthetic-process-pipe')
        reader.start()
        endpoint = None
        failure = None
        forced = False
        try:
            send({'kind': 'init', 'lane': lane, 'settings': wire_settings})
            assert ready.wait(40) and 'port' in state and not errors, errors
            endpoint = ('127.0.0.1', state['port'])
            with _ENDPOINT_LOCK:
                assert endpoint not in _OWNED_ENDPOINTS
                _OWNED_ENDPOINTS[endpoint] = original
            with httpx.Client(base_url='http://127.0.0.1:' + str(state['port']),
                              headers=headers, timeout=30, trust_env=False) as current:
                yield current
        except BaseException as error:
            failure = error
            raise
        finally:
            if endpoint is not None:
                with _ENDPOINT_LOCK:
                    assert _OWNED_ENDPOINTS.pop(endpoint) is original
            if original.poll() is None:
                try:
                    send({'kind': 'stop'})
                except (BrokenPipeError, OSError) as error:
                    errors.append(type(error).__name__ + ':stop channel closed')
            try:
                code = original.wait(timeout=25)
            except subprocess.TimeoutExpired:
                forced = True
                original.terminate()
                try:
                    code = original.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    original.kill()
                    code = original.wait()
            try:
                original.stdin.close()
            except BrokenPipeError:
                errors.append('BrokenPipeError:original stdin close')
            reader.join(5)
            original.stdout.close()
            receipt = {'native_exit': code, 'original_Popen_wait_returned': True,
                       'forced_failure_cleanup': forced, 'reader_closed': not reader.is_alive(),
                       'actual_child_closure': state.get('closure'), 'errors': errors,
                       'observations': observations, 'original_provider_exceptions': original_provider_exceptions,
                       'old_original_App_not_closed_by_helper': True,
                       'original_old_owner_closed_after': old_owner.closed,
                       'owned_endpoint_registration_removed': endpoint not in _OWNED_ENDPOINTS}
            (record / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
            if failure is None:
                assert not forced and code == 0 and not reader.is_alive() and not errors, receipt
                assert old_owner.closed is False and endpoint not in _OWNED_ENDPOINTS, receipt
                assert closed.is_set() and state['closure']['actual_owner_closed'] is True, receipt


def _child():
    # Preserve a dedicated original Popen protocol pipe; every ordinary Python
    # or native stdout diagnostic goes to the separate original stderr log.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    initialize = _read(sys.stdin.buffer)
    assert initialize['kind'] == 'init' and initialize['lane'] in LANES
    assert type(initialize['seq']) is int and initialize['seq'] == 1
    assert initialize['parent_pid'] == os.getppid()
    nonce = initialize['nonce']
    send_lock = threading.RLock()
    child_sequence = [0]
    pending = {}
    pending_lock = threading.Lock()
    stop = threading.Event()
    state = {}

    def emit(value):
        with send_lock:
            child_sequence[0] += 1
            _line(protocol, {'nonce': nonce, 'original_pid': os.getpid(),
                             'seq': child_sequence[0], **value}, send_lock)

    def rpc(operation, value=None):
        key = uuid.uuid4().hex
        result = queue.Queue(maxsize=1)
        with pending_lock:
            pending[key] = result
        try:
            emit({'kind': 'rpc', 'id': key, 'op': operation, 'value': value})
            reply = result.get(timeout=25)
            if not reply['ok'] and reply.get('exception') == 'JSONDecodeError':
                assert operation == 'http' and type(reply['msg']) is str and type(reply['doc']) is str
                assert type(reply['pos']) is int and 0 <= reply['pos'] <= len(reply['doc'])
                raise json.JSONDecodeError(reply['msg'], reply['doc'], reply['pos'])
            assert reply['ok'], reply.get('error')
            return reply['value']
        finally:
            with pending_lock:
                pending.pop(key, None)

    control_errors = []

    def control():
        sequence = 1
        try:
            while True:
                message = _read(sys.stdin.buffer)
                if message is None:
                    raise AssertionError('original parent channel ended before stop')
                assert message['nonce'] == nonce and message['parent_pid'] == initialize['parent_pid']
                assert type(message['seq']) is int and message['seq'] == sequence + 1
                sequence += 1
                if message['kind'] == 'stop':
                    return
                assert message['kind'] == 'reply' and type(message['ok']) is bool
                with pending_lock:
                    target = pending[message['id']]
                target.put_nowait(message)
        except BaseException as error:
            control_errors.append(type(error).__name__)
        finally:
            stop.set()
            if 'server' in state:
                state['server'].should_exit = True

    controller = threading.Thread(target=control, name='synthetic-process-original-control')
    controller.start()
    def deny_external(event, args):
        if event == 'socket.getaddrinfo' and args and args[0] not in ('127.0.0.1', 'localhost', '::1', None):
            raise AssertionError('synthetic process external DNS forbidden')
        if event == 'socket.connect' and len(args) > 1 and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('synthetic process external socket forbidden')
    sys.addaudithook(deny_external)
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.local_media_ai import LocalMediaAI
    from test_writing_stage_review_consumers import advance_clock
    import pytest
    import uvicorn
    import anyio
    # Match the original already-used guard dependencies before freezing time.
    from home_study_agent import supply_gap_resume, oral_supply_execution, scheduled_writing_goal, learning_input_replan
    values = initialize['settings']
    for name, value in list(values.items()):
        if type(value) is dict:
            assert set(value) in ({'path'}, {'bytes'})
            values[name] = Path(value['path']) if 'path' in value else bytes.fromhex(value['bytes'])
    settings = Settings(**values)
    patch = pytest.MonkeyPatch()
    clock = advance_clock(patch, datetime.fromisoformat(rpc('clock')))

    def transport(request):
        value = rpc('http', {'method': request.method, 'url': str(request.url),
                            'headers': list(request.headers.multi_items()),
                            'body': base64.b64encode(request.content).decode()})
        return httpx.Response(value['status'], headers=value['headers'],
                              content=base64.b64decode(value['body'], validate=True))

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    if initialize['lane'] == 'primary_oral':
        original_complete = gateway.complete

        async def complete(request):
            if request.request_type == 'agent_plan':
                from test_cloud_first_agent import Planner
                command = await anyio.to_thread.run_sync(lambda: rpc('primary_plan'))
                return await Planner(command['control'], {'start_selector': command['start_selector']},
                                     command['quote']).complete(request)
            return await original_complete(request)
        patch.setattr(gateway, 'complete', complete)

    patch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **kw: tuple(rpc('asr')))
    # The original App intentionally stays alive. Enter the same original
    # serialized startup owner before advisory initialize, not an unchecked retry.
    from home_study_agent.database import Database
    from home_study_agent.restore_publication import publication_owner
    startup_database = Database(settings.database_path, media_root=settings.data_dir,
                                data_encryption_key=settings.data_encryption_key,
                                runtime_mode=settings.runtime_mode)
    with publication_owner(startup_database, timeout=2):
        application = create_app(settings, model_gateway=gateway)
    clock = advance_clock(patch, max(clock.current, datetime.fromisoformat(rpc('clock'))))

    @application.middleware('http')
    async def original_learning_clock(request, call_next):
        clock.current = max(clock.current, datetime.fromisoformat(await anyio.to_thread.run_sync(lambda: rpc('clock'))))
        try:
            return await call_next(request)
        finally:
            current = await anyio.to_thread.run_sync(lambda: rpc('clock_advance', clock.current.isoformat()))
            clock.current = max(clock.current, datetime.fromisoformat(current))

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False))
        state['server'] = server
        def announce():
            until = time.monotonic() + 35
            while not server.started and not stop.is_set() and time.monotonic() < until:
                time.sleep(.01)
            if server.started:
                clock.current = max(clock.current, datetime.fromisoformat(rpc('clock_advance', clock.current.isoformat())))
                owner = application.state.money_owner
                emit({'kind': 'ready', 'port': listener.getsockname()[1], 'original_pid': os.getpid(),
                      'source_package_root': str(Path(sys.modules['home_study_agent.app'].__file__).resolve().parents[1]),
                      'database_path': str(application.state.repository.database.path.resolve()),
                      'actual_owner_active': owner.active, 'actual_owner_closed': owner.closed})
        announcer = threading.Thread(target=announce, name='actual-child-ready')
        announcer.start()
        server.run(sockets=[listener])
        announcer.join(2)
    controller.join(2)
    assert not controller.is_alive() and not announcer.is_alive() and not control_errors
    emit({'kind': 'closed', 'actual_owner_closed': application.state.money_owner.closed})
    patch.undo()
    protocol.close()


if __name__ == '__main__':
    assert sys.argv[1:] == ['--child']
    _child()

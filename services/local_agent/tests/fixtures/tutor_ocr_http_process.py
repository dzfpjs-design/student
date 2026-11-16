"""Fixed synthetic Tutor host; one real App, one original Gateway, no RPC calls."""
import contextlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import sys
import threading
import traceback
from uuid import UUID

root_arg, mode, model, nonce = sys.argv[1:]
root = Path(root_arg).resolve(strict=True)
assert root.name.startswith('test_') and any(p.name.startswith('pytest-') for p in root.parents)
assert str(UUID(nonce)) == nonce
assert os.environ['HSC_DATA_DIR'] == str(root)
assert os.environ['HSC_DATABASE_PATH'] == str(root/'test.db')
assert (mode, model) in {('blocking','blocking-test'),('blocking','old-attempt'),
                        ('blocking','confirmed-retry'),('replay','fake-tutor-v1')}


def offline(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1','::1','localhost'}:
            raise RuntimeError('SYNTHETIC_TUTOR_EXTERNAL_CONNECT_DENIED')
    elif event == 'socket.getaddrinfo' and args[0] not in {'127.0.0.1','::1','localhost'}:
        raise RuntimeError('SYNTHETIC_TUTOR_EXTERNAL_DNS_DENIED')
    elif event in {'subprocess.Popen','os.system','os.posix_spawn'}:
        raise RuntimeError('SYNTHETIC_TUTOR_NESTED_PROCESS_DENIED')

sys.addaudithook(offline)
wire = sys.stdout
lock = threading.RLock()
count = 0


def emit(kind, **value):
    global count
    raw = (json.dumps(dict(nonce=nonce,pid=os.getpid(),kind=kind,**value),ensure_ascii=False)+'\n').encode('utf-8')
    with lock:
        count += 1
        assert len(raw) <= 65536 and count <= 32
        wire.buffer.write(raw); wire.flush()


def run():
    # Ordinary module output stays in the held private stderr, not in IPC.
    from test_tutor_ocr_evidence import BlockingModelGateway, _tutor_http_client
    from test_app import FakeModelGateway
    from home_study_agent import money_egress as money
    assert not money.slot_present(), 'Synthetic Tutor must not use an active money slot'
    gateway = BlockingModelGateway(model) if mode == 'blocking' else FakeModelGateway()
    projected = 0
    def messages():
        nonlocal projected
        while projected < len(gateway.messages):
            request = gateway.messages[projected]
            projection = asdict(request)
            assert 'pre_dispatch' not in projection
            emit('request',index=projected,request=projection)
            projected += 1
    if mode == 'blocking':
        for kind in ('started','returned'):
            event = getattr(gateway,kind)
            original_set = event.set
            def observed_set(kind=kind, original_set=original_set):
                original_set()
                with lock:
                    messages(); emit(kind)
            event.set = observed_set
    else:
        original_complete = gateway.complete
        async def observed_complete(request):
            try:
                return await original_complete(request)
            finally:
                with lock:messages()
        gateway.complete = observed_complete
    cleanup_release = False
    manager = _tutor_http_client(root,gateway)
    with manager as client:
        # These are the exact original helper's objects, never a second App.
        server = manager.gen.gi_frame.f_locals['server']
        http_thread = manager.gen.gi_frame.f_locals['worker']
        owner = server.config.app.state.money_owner
        assert not owner.active and owner.budget is None and owner.latch is None
        emit('ready',port=client.base_url.port)
        stopped = False
        for line in sys.stdin:
            assert line in {'release\n','stop\n'}
            if line == 'release\n':
                assert mode == 'blocking'; gateway.release.set()
            else:
                stopped = True; break
        assert stopped, 'Parent did not request a normal stop'
        if mode == 'blocking' and gateway.started.is_set() and not gateway.returned.is_set():
            cleanup_release = True
            gateway.release.set()
    assert owner.closed and not owner._text_inflight and not owner._tts_inflight
    emit('closed',owner_closed=owner.closed,normal_only=not owner.active and owner.budget is None and owner.latch is None,
         http_drained=not http_thread.is_alive(),http_force_exit=bool(server.force_exit),cleanup_release=cleanup_release)

try:
    with contextlib.redirect_stdout(sys.stderr):
        run()
except BaseException:
    traceback.print_exc(file=sys.stderr)
    emit('error',code='TUTOR_CHILD_FAILED')
    raise SystemExit(1)

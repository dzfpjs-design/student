"""Regression for the Windows stdlib socketpair fallback under network denial."""
import asyncio
import socket
import threading

import pytest

from socketpair_network_guard import install_socketpair_only_network_guard


def emulated_stdlib_fallback(monkeypatch,hook):
    # Compile the same shape as the Windows fallback into socket's namespace;
    # the installed production fixture still captures its actual stdlib function.
    monkeypatch.setattr(socket,'_hsc_test_hook',hook,raising=False)
    monkeypatch.setattr(socket,'_hsc_test_destination',None,raising=False)
    monkeypatch.setattr(socket,'_hsc_test_pair',None,raising=False)
    code=compile('''def _hsc_test_pair():
    lsock=socket(AF_INET,SOCK_STREAM)
    csock=socket(AF_INET,SOCK_STREAM)
    try:
        lsock.bind(('127.0.0.1',0))
        lsock.listen(1)
        _hsc_test_hook()
        csock.connect(_hsc_test_destination or lsock.getsockname())
        accepted,_=lsock.accept()
        return accepted,csock
    except BaseException:
        csock.close()
        raise
    finally:
        lsock.close()
''',socket.__file__,'exec')
    exec(code,vars(socket))
    monkeypatch.setattr(socket,'socketpair',socket._hsc_test_pair)


def denied(address, *, method='connect'):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
        with pytest.raises(AssertionError,match='EXTERNAL_NETWORK_FORBIDDEN'):
            getattr(sock,method)(address)


def test_real_socketpair_and_event_loop_work_but_ordinary_connections_are_denied(monkeypatch):
    blocked=[]
    install_socketpair_only_network_guard(monkeypatch,blocked)
    left,right=socket.socketpair()
    try:
        left.sendall(b'x');assert right.recv(1)==b'x'
        assert asyncio.run(asyncio.sleep(0,result='ok'))=='ok'
    finally:
        left.close();right.close()
    denied(('127.0.0.1',1234));denied(('203.0.113.10',443));denied(('127.0.0.1',1234),method='connect_ex')
    assert len(blocked)==3


def test_windows_fallback_exception_is_exact_frame_thread_and_nested_only(monkeypatch):
    blocked=[];depth=[0];other_thread=[]
    def hook():
        if depth[0]:return
        depth[0]+=1
        denied(('127.0.0.1',1234));denied(('203.0.113.10',443))
        def worker():
            denied(('127.0.0.1',1234));other_thread.append(True)
        thread=threading.Thread(target=worker);thread.start();thread.join(timeout=2)
        assert not thread.is_alive() and other_thread==[True]
        left,right=socket.socketpair();left.close();right.close()
    emulated_stdlib_fallback(monkeypatch,hook)
    install_socketpair_only_network_guard(monkeypatch,blocked)
    left,right=socket.socketpair();left.close();right.close()
    denied(('127.0.0.1',1234))
    assert len(blocked)==4


def test_exception_restores_narrow_grant_and_nonstdlib_pair_cannot_bypass(monkeypatch):
    blocked=[]
    def broken():raise RuntimeError('synthetic pair failure')
    emulated_stdlib_fallback(monkeypatch,broken)
    install_socketpair_only_network_guard(monkeypatch,blocked)
    with pytest.raises(RuntimeError,match='synthetic pair failure'):
        socket.socketpair()
    denied(('127.0.0.1',1234))
    assert len(blocked)==1


def test_replaced_nonstdlib_socketpair_gets_no_loopback_grant(monkeypatch):
    blocked=[]
    def nonstdlib_pair():
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.connect(('127.0.0.1',1234))
    monkeypatch.setattr(socket,'socketpair',nonstdlib_pair)
    install_socketpair_only_network_guard(monkeypatch,blocked)
    with pytest.raises(AssertionError,match='EXTERNAL_NETWORK_FORBIDDEN'):
        socket.socketpair()
    assert len(blocked)==1


def test_stdlib_frame_does_not_allow_dns_name_or_external_address(monkeypatch):
    blocked=[]
    emulated_stdlib_fallback(monkeypatch,lambda:None)
    install_socketpair_only_network_guard(monkeypatch,blocked)
    for address in (('localhost',1234),('127.0.0.1',1234),('203.0.113.10',443)):
        monkeypatch.setattr(socket,'_hsc_test_destination',address)
        with pytest.raises(AssertionError,match='EXTERNAL_NETWORK_FORBIDDEN'):
            socket.socketpair()
    assert len(blocked)==3

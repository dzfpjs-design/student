"""Test-only network denial with a narrow stdlib socketpair exception."""
import socket
import sys
import threading
import os


def install_socketpair_only_network_guard(monkeypatch, blocked):
    original_pair=socket.socketpair
    pair_code=getattr(original_pair,'__code__',None)
    native_pair = (pair_code is not None and getattr(original_pair,'__module__',None)=='socket'
                   and getattr(original_pair,'__globals__',None) is vars(socket)
                   and os.path.normcase(os.path.abspath(pair_code.co_filename)) == os.path.normcase(os.path.abspath(socket.__file__)))
    original_connect=socket.socket.connect
    state=threading.local()

    def guarded_pair(*args,**kwargs):
        depth=getattr(state,'depth',0)
        state.depth=depth+1
        try:
            return original_pair(*args,**kwargs)
        finally:
            state.depth=depth

    def guarded_connect(sock,address):
        # Windows' stdlib fallback connects directly from socketpair() to its
        # own loopback listener. No other frame or thread receives this grant.
        caller=sys._getframe(1)
        local=(isinstance(address,tuple) and len(address) in (2,4)
               and address[0] in ('127.0.0.1','::1') and type(address[1]) is int
               and 0 < address[1] <= 65535 and sock.family in (socket.AF_INET,socket.AF_INET6))
        if native_pair and getattr(state,'depth',0)>0 and caller.f_code is pair_code and local:
            listener=caller.f_locals.get('lsock')
            try:
                same_listener=(isinstance(listener,socket.socket) and caller.f_locals.get('csock') is sock
                    and listener.family==sock.family and listener.getsockname()[:2]==address[:2])
            except (OSError,TypeError,ValueError):
                same_listener=False
            if same_listener:
                return original_connect(sock,address)
        blocked.append(True)
        raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')

    def deny(*args,**kwargs):
        blocked.append(True)
        raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')

    monkeypatch.setattr(socket,'socketpair',guarded_pair)
    monkeypatch.setattr(socket.socket,'connect',guarded_connect)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)

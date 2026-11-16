"""Owned SMTP subprocess protocol with an independent public transport gate.

All credentials and message bytes arrive over the inherited private pipe, never
arguments or environment. The current transport safety gate permits only
addresses resolved to loopback; the configured hostname remains the TLS name.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import math
import re
import socket
import smtplib
import ssl
import struct
import sys
import time

MAX_FRAME = 20 * 1024 * 1024


def _read_exact(stream, count):
    chunks = []
    while count:
        part = stream.read(min(count, 65536))
        if not part:
            raise EOFError()
        chunks.append(part)
        count -= len(part)
    return b''.join(chunks)


def _frame(stream):
    length = struct.unpack('!I', _read_exact(stream, 4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ValueError('FRAME_INVALID')
    value = json.loads(_read_exact(stream, length))
    if type(value) is not dict:
        raise ValueError('FRAME_INVALID')
    return value


def _line(stream, value):
    stream.write(value + b'\n')
    stream.flush()


def _valid_host(host):
    if type(host) is not str or not host or len(host) > 253 or host != host.strip():
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if re.fullmatch(r'[0-9.]+', host):
        return False
    return all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
               for label in host.split('.'))


def _public_transport_proven():
    # Independent onedir worker boundary: requests and the parent environment
    # cannot enable Internet access. Opening this requires a reviewed rebuild.
    return False


def _public_domain(host):
    if not _valid_host(host) or '.' not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _public_ip(address):
    # is_global alone admits multicast on some Python versions.
    return bool(address.is_global and not (address.is_multicast or address.is_loopback
        or address.is_private or address.is_link_local or address.is_unspecified
        or address.is_reserved or getattr(address, 'ipv4_mapped', None)
        or getattr(address, 'sixtofour', None) or getattr(address, 'teredo', None)))


def _pinned_target(host, port, *, public=False):
    # Resolve inside the disposable, deadline-controlled worker and check the
    # complete answer before connecting to exactly one numeric address.
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 32:
        raise ValueError('SMTP_DNS_UNAVAILABLE')
    targets = []
    for family, kind, protocol, _canonical, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
            raise ValueError('SMTP_DNS_INVALID')
        numeric = ipaddress.ip_address(address[0])
        allowed = _public_ip(numeric) if public else numeric.is_loopback
        if not allowed or address[1] != port or (family == socket.AF_INET6 and address[3] != 0):
            raise ValueError('SMTP_LOOPBACK_ONLY')
        targets.append((family, kind, protocol, address))
    return sorted(targets, key=lambda item: item[0] != socket.AF_INET)[0]


def _pinned_loopback(host, port):
    return _pinned_target(host, port)


def _open_pinned(target, timeout):
    family, kind, protocol, address = target
    sock = socket.socket(family, kind, protocol)
    try:
        sock.settimeout(timeout)
        sock.connect(address)
        return sock
    except BaseException:
        sock.close()
        raise


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port, *, target, timeout):
        self._target = target
        super().__init__(host, port, timeout=timeout, local_hostname='[127.0.0.1]')

    def _get_socket(self, host, port, timeout):
        return _open_pinned(self._target, timeout)


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(self, host, port, *, target, timeout, context):
        self._target = target
        super().__init__(host, port, timeout=timeout, context=context, local_hostname='[127.0.0.1]')

    def _get_socket(self, host, port, timeout):
        sock = _open_pinned(self._target, timeout)
        try:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        except BaseException:
            sock.close()
            raise


def run(stdin, stdout):
    client = None
    try:
        request = _frame(stdin)
        required = {'host', 'port', 'username', 'password', 'sender', 'recipient', 'mime', 'deadline', 'ssl', 'starttls'}
        if set(request) not in (required, required | {'public'}):
            return 2
        host = request['host']
        if not _valid_host(host):
            return 2
        public = request.get('public', False)
        if type(public) is not bool or (public and (not _public_transport_proven() or not _public_domain(host))):
            return 2
        if (type(request['port']) is not int or not 1 <= request['port'] <= 65535
                or type(request['deadline']) not in (int, float) or not math.isfinite(request['deadline'])
                or type(request['ssl']) is not bool or type(request['starttls']) is not bool
                or (request['ssl'] and request['starttls'])
                or (public and not (request['ssl'] or request['starttls']))
                or any(type(request[k]) is not str or len(request[k]) > 2048 for k in ('username','password','sender','recipient'))
                or not request['sender'] or not request['recipient']
                or bool(request['username'].strip()) != bool(request['password'])
                or (request['username'].strip() and not (request['ssl'] or request['starttls']))):
            return 2
        message = base64.b64decode(request['mime'], validate=True)
        if not message or len(message) > 14 * 1024 * 1024:
            return 2
        left = request['deadline'] - time.monotonic()
        if left <= 0:
            return 3
        target = _pinned_target(host, request['port'], public=public)
        left = request['deadline'] - time.monotonic()
        if left <= 0:
            return 3
        timeout = min(15.0, left)
        if request['ssl']:
            client = _PinnedSMTPSSL(host, request['port'], target=target, timeout=timeout, context=ssl.create_default_context())
        else:
            client = _PinnedSMTP(host, request['port'], target=target, timeout=timeout)
            if request['starttls']:
                client.starttls(context=ssl.create_default_context())
        if request['username']:
            client.login(request['username'], request['password'])
        code, _ = client.mail(request['sender'])
        if not 200 <= code < 300:
            return 4
        code, _ = client.rcpt(request['recipient'])
        if not 200 <= code < 300:
            return 4
        _line(stdout, b'READY')
        if stdin.readline(4) != b'GO\n' or time.monotonic() >= request['deadline']:
            return 5
        code, _ = client.data(message)
        if 200 <= code < 300:
            _line(stdout, b'ACCEPTED')
            return 0
        _line(stdout, b'REJECTED')
        return 6
    except Exception:
        # SMTP exceptions may contain addresses or server text; never log them.
        return 7
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def main():
    if sys.argv[1:] == ['--public-readiness']:
        if _public_transport_proven():
            sys.stdout.buffer.write(b'HSC_SMTP_PUBLIC_READY_V1\n')
            sys.stdout.buffer.flush()
            return
        raise SystemExit(2)
    raise SystemExit(run(sys.stdin.buffer, sys.stdout.buffer))


if __name__ == '__main__':
    main()

"""Exercise the real owned worker through pipes against synthetic loopback TLS."""
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
import base64
import json
import os
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

WORKER = Path(__file__).resolve().parents[1] / 'src/home_study_agent/smtp_owned_worker.py'
USER = 'synthetic-user'
PASSWORD = 'synthetic-private-password'
MESSAGE = b'Subject: synthetic\r\n\r\nSynthetic test message.\r\n'


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic Test CA')])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_path = tmp_path / 'synthetic-ca.pem'
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    def leaf(ip_san):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic.local')])
        san = ([x509.IPAddress(ip_address('127.0.0.1'))] if ip_san else
               [x509.DNSName('localhost'), x509.DNSName('smtp.synthetic.test')])
        cert = (x509.CertificateBuilder().subject_name(server_name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName(san), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(ca_key, hashes.SHA256()))
        cert_path, key_path = tmp_path / f'leaf-{ip_san}.pem', tmp_path / f'key-{ip_san}.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        return context
    return ca_path, leaf


class SmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self, context, *, implicit=False, advertise_starttls=True, auth_ok=True, auth_mode='PLAIN'):
        self.context = context
        self.implicit = implicit
        self.advertise_starttls = advertise_starttls
        self.auth_ok = auth_ok
        self.auth_mode = auth_mode
        self.sni = []
        self.auth = []
        self.data = []
        self.commands = []
        self.errors = []
        context.set_servername_callback(lambda _sock, name, _ctx: self.sni.append(name))
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        encrypted = False
        try:
            if self.server.implicit:
                self._wrap()
                encrypted = True
            self._send(b'220 synthetic.local ESMTP\r\n')
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                verb = line.split(b' ', 1)[0].strip().upper()
                self.server.commands.append(verb)
                if verb in (b'EHLO', b'HELO'):
                    extensions = b'250-synthetic.local\r\n'
                    if not encrypted and self.server.advertise_starttls:
                        extensions += b'250-STARTTLS\r\n'
                    if encrypted:
                        extensions += b'250-AUTH ' + self.server.auth_mode.encode() + b'\r\n'
                    self._send(extensions + b'250 SIZE 1048576\r\n')
                elif verb == b'STARTTLS':
                    self._send(b'220 Ready for synthetic TLS\r\n')
                    self._wrap()
                    encrypted = True
                elif verb == b'AUTH':
                    encoded = line.strip().split(b' ', 2)[-1]
                    try: decoded = base64.b64decode(encoded, validate=True)
                    except Exception: decoded = b''
                    if self.server.auth_mode == 'LOGIN':
                        self._send(b'334 UGFzc3dvcmQ6\r\n')
                        try: supplied_password = base64.b64decode(self.rfile.readline(65536).strip(), validate=True)
                        except Exception: supplied_password = b''
                        decoded = b'\x00' + decoded + b'\x00' + supplied_password
                    self.server.auth.append((encrypted, decoded))
                    self._send(b'235 Authentication successful\r\n' if self.server.auth_ok else b'535 Authentication failed\r\n')
                elif verb in (b'MAIL', b'RCPT'):
                    self._send(b'250 OK\r\n')
                elif verb == b'DATA':
                    self._send(b'354 Continue\r\n')
                    lines = []
                    while True:
                        part = self.rfile.readline(65536)
                        if not part or part == b'.\r\n': break
                        lines.append(part)
                    self.server.data.append(b''.join(lines))
                    self._send(b'250 Queued synthetic message\r\n')
                else:
                    self._send(b'250 OK\r\n')
        except Exception as exc:
            # Untrusted peers close during handshake; retain only a synthetic
            # error class for diagnostics, never echo SMTP payloads.
            self.server.errors.append(type(exc).__name__)
            return
    def _send(self, data):
        self.wfile.write(data)
        self.wfile.flush()
    def _wrap(self):
        self.rfile.close()
        self.wfile.close()
        self.connection = self.server.context.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile('rb', self.rbufsize)
        self.wfile = self.connection.makefile('wb', self.wbufsize)


@pytest.fixture
def smtp_factory():
    running = []
    def start(context, **kwargs):
        server = SmtpServer(context, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server
    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def launch(port, *, ca=None, implicit=False, starttls=False, username=USER, password=PASSWORD,
           host='127.0.0.1', synthetic_dns=None, public=False, allow_public_fixture=False):
    env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR'}}
    if ca is not None or synthetic_dns is not None or allow_public_fixture:
        # Isolated test-only root CA injection; worker still uses an ordinary
        # CERT_REQUIRED, hostname-checking context. No secrets in arguments.
        harness = ("import ssl,runpy,sys,socket; original=ssl.create_default_context; "
                   "ssl.create_default_context=lambda: configure(original()); "
                   "exec('def configure(ctx):\\n if sys.argv[2]: ctx.load_verify_locations(cafile=sys.argv[2])\\n return ctx'); "
                   "real=socket.getaddrinfo; calls=[]; "
                   "exec('def resolve(host, port, *args, **kwargs):\\n if sys.argv[3] and host.endswith(\".synthetic.test\"):\\n  calls.append(host)\\n  return real(sys.argv[3] if len(calls)==1 else \"192.0.2.1\", port, *args, **kwargs)\\n return real(host, port, *args, **kwargs)'); "
                   "socket.getaddrinfo=resolve; "
                   "exec('real_connect=socket.socket.connect\\ndef connect(s,addr):\\n if addr[0]!=\"93.184.216.34\": raise RuntimeError(\"external blocked\")\\n return real_connect(s,(\"127.0.0.1\",addr[1]))'); "
                   "exec('if sys.argv[4]==\"yes\":\\n socket.socket.connect=connect\\n m=runpy.run_path(sys.argv[1],run_name=\"isolated_worker_test\")\\n m[\"run\"].__globals__[\"_public_transport_proven\"]=lambda:True\\n sys.exit(m[\"run\"](sys.stdin.buffer,sys.stdout.buffer))\\nelse:\\n runpy.run_path(sys.argv[1],run_name=\"__main__\")')")
        args = [sys.executable, '-I', '-S', '-B', '-c', harness, str(WORKER), str(ca or ''), synthetic_dns or '', 'yes' if allow_public_fixture else 'no']
    else:
        args = [sys.executable, '-I', '-S', '-B', str(WORKER)]
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, close_fds=True)
    request = {'host': host, 'port': port, 'username': username, 'password': password,
               'sender': 'sender@example.test', 'recipient': 'recipient@example.test',
               'mime': base64.b64encode(MESSAGE).decode(), 'deadline': time.monotonic() + 12,
               'ssl': implicit, 'starttls': starttls, 'public': public}
    encoded = json.dumps(request).encode()
    process.stdin.write(struct.pack('!I', len(encoded)) + encoded)
    process.stdin.flush()
    assert USER not in ' '.join(args) and PASSWORD not in ' '.join(args)
    assert all(USER not in v and PASSWORD not in v for v in env.values())
    return process


def finish(process):
    try:
        return process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
@pytest.mark.parametrize('auth_mode', ['PLAIN', 'LOGIN'])
def test_authenticated_tls_and_submission_handshake(certificates, smtp_factory, mode, auth_mode):
    ca, make_context = certificates
    server = smtp_factory(make_context(True), implicit=mode == 'implicit', auth_mode=auth_mode)
    process = launch(server.server_address[1], ca=ca, implicit=mode == 'implicit', starttls=mode == 'starttls')
    try:
        assert process.stdout.readline() == b'READY\n', (server.sni, server.commands, server.auth, server.errors, process.poll())
        assert server.auth == [(True, b'\x00' + USER.encode() + b'\x00' + PASSWORD.encode())]
        assert server.data == []
        process.stdin.write(b'GO\n'); process.stdin.flush()
        assert process.stdout.readline() == b'ACCEPTED\n'
        stdout, stderr = finish(process)
        assert process.returncode == 0 and stdout == b'' and stderr == b''
        assert len(server.data) == 1 and b'Synthetic test message.' in server.data[0]
        assert server.sni == [None]  # Python suppresses SNI for a numeric IP.
    finally:
        if process.poll() is None: process.kill(); process.communicate()


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
@pytest.mark.parametrize('problem', ['untrusted', 'wrong_ip', 'auth_rejected'])
def test_tls_or_auth_failure_never_reaches_data(certificates, smtp_factory, mode, problem):
    ca, make_context = certificates
    server = smtp_factory(make_context(problem != 'wrong_ip'), implicit=mode == 'implicit', auth_ok=problem != 'auth_rejected')
    process = launch(server.server_address[1], ca=None if problem == 'untrusted' else ca,
                     implicit=mode == 'implicit', starttls=mode == 'starttls')
    stdout, stderr = finish(process)
    assert process.returncode != 0 and b'READY' not in stdout and stderr == b''
    assert not server.data and b'DATA' not in server.commands
    if problem != 'auth_rejected': assert not server.auth


def test_starttls_absence_never_downgrades_or_authenticates(certificates, smtp_factory):
    ca, make_context = certificates
    server = smtp_factory(make_context(True), advertise_starttls=False)
    process = launch(server.server_address[1], ca=ca, starttls=True)
    stdout, stderr = finish(process)
    assert process.returncode != 0 and b'READY' not in stdout and stderr == b''
    assert not server.auth and not server.data and b'DATA' not in server.commands


def test_conflicting_tls_modes_are_rejected_before_connect(certificates, smtp_factory):
    ca, make_context = certificates
    server = smtp_factory(make_context(True), implicit=True)
    process = launch(server.server_address[1], ca=ca, implicit=True, starttls=True)
    stdout, stderr = finish(process)
    assert process.returncode == 2 and stdout == b'' and stderr == b''
    assert server.commands == [] and server.auth == [] and server.data == []


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
def test_dns_address_is_pinned_and_tls_uses_original_name(certificates, smtp_factory, mode):
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=mode == 'implicit')
    process = launch(server.server_address[1], ca=ca, host='smtp.synthetic.test',
                     synthetic_dns='127.0.0.1', implicit=mode == 'implicit', starttls=mode == 'starttls')
    try:
        assert process.stdout.readline() == b'READY\n'
        assert server.sni == ['smtp.synthetic.test']
        assert server.auth == [(True, b'\x00' + USER.encode() + b'\x00' + PASSWORD.encode())]
        process.stdin.write(b'GO\n'); process.stdin.flush()
        assert process.stdout.readline() == b'ACCEPTED\n'
        finish(process)
        assert process.returncode == 0 and len(server.data) == 1
    finally:
        if process.poll() is None: process.kill(); process.communicate()


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
def test_dns_certificate_name_mismatch_never_authenticates(certificates, smtp_factory, mode):
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=mode == 'implicit')
    process = launch(server.server_address[1], ca=ca, host='wrong.synthetic.test',
                     synthetic_dns='127.0.0.1', implicit=mode == 'implicit', starttls=mode == 'starttls')
    stdout, stderr = finish(process)
    assert process.returncode != 0 and b'READY' not in stdout and stderr == b''
    assert server.sni == ['wrong.synthetic.test'] and not server.auth and not server.data


def test_dns_external_answer_is_rejected_before_connect(certificates, smtp_factory):
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=True)
    process = launch(server.server_address[1], ca=ca, host='smtp.synthetic.test',
                     synthetic_dns='192.0.2.1', implicit=True)
    stdout, stderr = finish(process)
    assert process.returncode != 0 and b'READY' not in stdout and stderr == b''
    assert server.sni == [] and not server.auth and not server.data


def test_mixed_dns_answers_are_rejected_before_any_connect(monkeypatch):
    from home_study_agent import smtp_owned_worker as worker
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 2525)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('192.0.2.1', 2525)),
    ])
    with pytest.raises(ValueError, match='SMTP_LOOPBACK_ONLY'):
        worker._pinned_loopback('smtp.synthetic.test', 2525)


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
def test_owned_process_passes_saved_domain_to_real_worker(certificates, smtp_factory, monkeypatch, mode):
    from contextlib import nullcontext
    from home_study_agent import smtp_owned_process as owner
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=mode == 'implicit')
    harness = (
        "import ssl,socket,runpy,sys; real=socket.getaddrinfo; "
        "socket.getaddrinfo=lambda h,p,*a,**k: real('127.0.0.1' if h=='smtp.synthetic.test' else h,p,*a,**k); "
        "original=ssl.create_default_context; "
        "exec('def context():\\n c=original()\\n c.load_verify_locations(cafile=sys.argv[2])\\n return c'); "
        "ssl.create_default_context=context; runpy.run_path(sys.argv[1],run_name='__main__')"
    )
    executable = getattr(sys, '_base_executable', sys.executable) if sys.platform == 'win32' else sys.executable
    monkeypatch.setattr(owner.OwnedSmtpProcess, '_worker_command',
                        lambda self: [executable, '-I', '-S', '-B', '-c', harness, str(WORKER), str(ca)])
    if sys.platform != 'win32':
        monkeypatch.setattr(owner, '_job_members', lambda job, process: [
            {'pid': process.pid, 'windows_creation_time_100ns': 1, 'role': 'worker', 'image_path': 'synthetic'}])
    persisted = []
    process = owner.OwnedSmtpProcess(nullcontext, lambda: True, lambda *args: persisted.append(True))
    result = process.submit(host='smtp.synthetic.test', port=server.server_address[1],
                            username=USER, password=PASSWORD, sender='sender@example.test',
                            recipient='recipient@example.test', mime_bytes=MESSAGE, timeout_seconds=8,
                            use_ssl=mode == 'implicit', starttls=mode == 'starttls')
    assert result == {'status': 'accepted', 'possible_submission': True, 'exit_confirmed': True}
    assert persisted == [True] and server.sni == ['smtp.synthetic.test'] and len(server.data) == 1


def test_stalled_dns_is_terminated_by_owner_deadline(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from home_study_agent import smtp_owned_process as owner
    marker = tmp_path / 'dns-entered'
    harness = ("import socket,runpy,sys,time; "
               "exec('def stall(*args,**kwargs):\\n open(sys.argv[2],\"w\").close()\\n time.sleep(30)'); "
               "socket.getaddrinfo=stall; runpy.run_path(sys.argv[1],run_name='__main__')")
    executable = getattr(sys, '_base_executable', sys.executable) if sys.platform == 'win32' else sys.executable
    monkeypatch.setattr(owner.OwnedSmtpProcess, '_worker_command',
                        lambda self: [executable, '-I', '-S', '-B', '-c', harness, str(WORKER), str(marker)])
    persisted = []
    process = owner.OwnedSmtpProcess(nullcontext, lambda: True, lambda *args: persisted.append(True))
    result = process.submit(host='smtp.synthetic.test', port=2525, username=USER, password=PASSWORD,
                            sender='sender@example.test', recipient='recipient@example.test', mime_bytes=MESSAGE,
                            timeout_seconds=1, use_ssl=True, starttls=False)
    process.thread.join(5)
    assert marker.exists() and not process.thread.is_alive()
    assert result['status'] in ('cleanup_pending', 'not_submitted')
    assert process.cleaned and process.confirmed and not process.intent and not persisted


def test_public_request_cannot_override_worker_source_gate(certificates, smtp_factory):
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=True)
    process = launch(server.server_address[1], ca=ca, host='smtp.synthetic.test',
                     synthetic_dns='93.184.216.34', implicit=True, public=True)
    stdout, stderr = finish(process)
    assert process.returncode == 2 and not stdout and not stderr
    assert not server.sni and not server.auth and not server.data


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
def test_public_dns_pins_address_and_uses_original_tls_name_in_isolated_harness(certificates, smtp_factory, mode):
    ca, make_context = certificates
    server = smtp_factory(make_context(False), implicit=mode == 'implicit')
    # This subprocess-only harness redirects the synthetic public address to
    # loopback and rejects every other socket destination; no external network.
    process = launch(server.server_address[1], ca=ca, host='smtp.synthetic.test',
                     synthetic_dns='93.184.216.34', implicit=mode == 'implicit', starttls=mode == 'starttls',
                     public=True, allow_public_fixture=True)
    try:
        assert process.stdout.readline() == b'READY\n'
        assert server.sni == ['smtp.synthetic.test'] and server.auth
        process.stdin.write(b'GO\n'); process.stdin.flush()
        assert process.stdout.readline() == b'ACCEPTED\n'
        finish(process)
        assert process.returncode == 0 and len(server.data)==1
    finally:
        if process.poll() is None: process.kill(); process.communicate()


@pytest.mark.parametrize('mode', ['implicit', 'starttls'])
@pytest.mark.parametrize('problem', ['wrong_name', 'untrusted'])
def test_public_tls_failure_cannot_authenticate_in_isolated_harness(certificates, smtp_factory, mode, problem):
    ca,make_context=certificates
    server=smtp_factory(make_context(False),implicit=mode=='implicit')
    process=launch(server.server_address[1],ca=None if problem=='untrusted' else ca,
                   host='wrong.synthetic.test' if problem=='wrong_name' else 'smtp.synthetic.test',
                   synthetic_dns='93.184.216.34',implicit=mode=='implicit',starttls=mode=='starttls',
                   public=True,allow_public_fixture=True)
    stdout,stderr=finish(process)
    assert process.returncode!=0 and b'READY' not in stdout and not stderr
    assert not server.auth and not server.data and b'DATA' not in server.commands


def test_public_plaintext_rejected_before_connect_in_isolated_harness(certificates, smtp_factory):
    ca,make_context=certificates
    server=smtp_factory(make_context(False))
    process=launch(server.server_address[1],ca=ca,host='smtp.synthetic.test',synthetic_dns='93.184.216.34',
                   username='',password='',public=True,allow_public_fixture=True)
    stdout,stderr=finish(process)
    assert process.returncode==2 and not stdout and not stderr
    assert not server.commands and not server.auth and not server.data


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.0.2.1',
                                    '224.0.0.1', '::1', 'fc00::1', 'fe80::1', 'ff02::1',
                                    '::ffff:8.8.8.8', '2002:0808:0808::1'])
def test_public_policy_rejects_nonpublic_and_transition_addresses(address):
    from home_study_agent.smtp_owned_worker import _public_ip
    assert not _public_ip(ip_address(address))


def test_public_dns_rejects_mixed_answers_even_when_one_is_global(monkeypatch):
    from home_study_agent import smtp_owned_worker as worker
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[
        (socket.AF_INET,socket.SOCK_STREAM,0,'',('93.184.216.34',2525)),
        (socket.AF_INET,socket.SOCK_STREAM,0,'',('169.254.169.254',2525)),
    ])
    with pytest.raises(ValueError):
        worker._pinned_target('smtp.synthetic.test',2525,public=True)

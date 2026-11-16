"""Single MIME + durable ambiguity, isolated loopback and synthetic data only."""
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
from socketserver import StreamRequestHandler, ThreadingTCPServer
import subprocess
import sys
from threading import Thread
from zipfile import ZipFile
import pytest
from home_study_agent import notification_channels
from combined_report_loopback_candidate import TABLE, recover, send
from test_combined_report_real_sources_fixture import synthetic_combined_family, OCR_TEXT
from test_daily_report_combined_snapshot import candidate


@contextmanager
def sink(*,lose_reply=False,on_connect=None,on_data=None):
    class Server(ThreadingTCPServer):
        allow_reuse_address=True; daemon_threads=True
    class Handler(StreamRequestHandler):
        def handle(self):
            if on_connect: on_connect()
            self.wfile.write(b'220 synthetic.test ESMTP\r\n')
            while True:
                line=self.rfile.readline()
                if not line: return
                command=line.split(None,1)[0].upper()
                if command in (b'EHLO',b'HELO'): self.wfile.write(b'250 synthetic.test\r\n')
                elif command in (b'MAIL',b'RCPT',b'RSET'): self.wfile.write(b'250 OK\r\n')
                elif command==b'DATA':
                    self.server.data_commands+=1
                    if on_data: on_data()
                    self.wfile.write(b'354 send data\r\n'); body=bytearray()
                    while True:
                        line=self.rfile.readline()
                        if not line: return
                        if line==b'.\r\n': break
                        body.extend(line)
                    self.server.messages.append(bytes(body))
                    if lose_reply: return
                    self.wfile.write(b'250 accepted\r\n')
                elif command==b'QUIT': self.wfile.write(b'221 bye\r\n'); return
                else: self.wfile.write(b'250 OK\r\n')
    server=Server(('127.0.0.1',0),Handler); server.messages=[]; server.data_commands=0
    thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield server
    finally: server.shutdown(); server.server_close(); thread.join(5)


def install(f):
    with f['db'].connect() as c: c.execute(TABLE); c.commit()


def assert_three_sources(message,expected_hash):
    parsed=BytesParser(policy=policy.default).parsebytes(message)
    assert parsed['To']=='parent@example.test'
    text=parsed.get_body(preferencelist=('plain',)).get_content()
    assert '判别式' in text and OCR_TEXT in text
    attachments=[a for a in parsed.iter_attachments() if a.get_content_type()=='application/zip']
    assert len(attachments)==1 and sha256(attachments[0].get_payload(decode=True)).hexdigest()==expected_hash
    with ZipFile(BytesIO(attachments[0].get_payload(decode=True))) as archive:
        assert set(archive.namelist())=={'manifest.json','events.jsonl'}
        assert b'PRIVATE_SYNTHETIC_TRANSCRIPT_SHOULD_NOT_BE_IN_ZIP' not in archive.read('events.jsonl')


def test_one_real_mime_has_all_three_sources_and_no_writer_across_network(candidate,monkeypatch):
    f,batch,snapshot,_=candidate; install(f)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    with f['db'].connect() as c: ziphash=c.execute('SELECT attachment_sha256 FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch,)).fetchone()[0]
    writer_check=[]
    def on_data():
        with f['db'].connect() as c:
            c.execute('BEGIN IMMEDIATE'); writer_check.append(True); c.rollback()
    with sink(on_data=on_data) as smtp:
        assert send(f['telemetry'],snapshot,smtp.server_address[1])=='sent'
        assert len(smtp.messages)==1 and writer_check==[True]
        assert_three_sources(smtp.messages[0],ziphash)
        with pytest.raises(ValueError,match='PRIOR_ATTEMPT'): send(f['telemetry'],snapshot,smtp.server_address[1])
        assert len(smtp.messages)==1
    with f['db'].connect() as c: assert c.execute('SELECT status FROM combined_loopback_attempt').fetchone()[0]=='sent'


def test_source_change_during_handshake_callback_stops_before_data(candidate,monkeypatch):
    f,batch,snapshot,_=candidate; install(f)
    monkeypatch.setattr(notification_channels,'_admit_external_delivery_budget',lambda:None)
    def on_connect():
        with f['db'].connect() as c:
            c.execute("UPDATE parent_report_snapshot SET status='revoked' WHERE snapshot_id=?",(f['formal']['snapshot_id'],)); c.commit()
    with sink(on_connect=on_connect) as smtp:
        assert send(f['telemetry'],snapshot,smtp.server_address[1])=='unknown'
        assert smtp.messages==[] and smtp.data_commands==0


def test_lost_data_reply_then_subprocess_crash_recovers_unknown_without_repeat(candidate):
    f,batch,snapshot,tmp_path=candidate; install(f)
    with f['db'].connect() as c: ziphash=c.execute('SELECT attachment_sha256 FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch,)).fetchone()[0]
    with sink(lose_reply=True) as smtp:
        from combined_report_loopback_candidate import __file__ as worker
        process=subprocess.run([sys.executable,worker,str(f['db'].path),str(tmp_path),snapshot,str(smtp.server_address[1])],capture_output=True,timeout=30)
        assert process.returncode==91, process.stderr.decode(errors='replace')[-1000:]
        assert len(smtp.messages)==1
        assert_three_sources(smtp.messages[0],ziphash)
        with f['db'].connect() as c: assert c.execute('SELECT status FROM combined_loopback_attempt').fetchone()[0]=='processing'
        assert recover(f['telemetry'])==1 and recover(f['telemetry'])==0
        with f['db'].connect() as c: assert c.execute('SELECT status FROM combined_loopback_attempt').fetchone()[0]=='unknown'
        with pytest.raises(ValueError,match='PRIOR_ATTEMPT'): send(f['telemetry'],snapshot,smtp.server_address[1])
        assert len(smtp.messages)==1

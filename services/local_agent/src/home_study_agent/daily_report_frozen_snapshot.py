"""Encrypted local daily observation snapshots, with no mail transport.

The external-effect guard serializes consent, deletion, restore, and this local
freeze/check. It is never used to hold a SQLite transaction over network I/O.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .daily_report_observation import _authority, _projection, _source_digest

SNAPSHOT_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_observation_report_snapshot (
 snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
 child_id TEXT NOT NULL, day TEXT NOT NULL, recipient_id TEXT NOT NULL,
 generated_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL, encrypted_payload BLOB NOT NULL,
 status TEXT NOT NULL CHECK(status='frozen_local'),
 FOREIGN KEY(source_id,child_id,day) REFERENCES daily_observation_report_source(source_id,child_id,day) ON DELETE CASCADE
)"""


def schema_valid(c):
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_observation_report_snapshot'").fetchone()
    normalize = lambda sql: ' '.join(sql.replace('IF NOT EXISTS ', '').split())
    return bool(row and normalize(row[0]) == normalize(SNAPSHOT_SCHEMA))


def _aad(snapshot_id, source_id, child_id, day, recipient_id):
    return json.dumps(['daily_observation_frozen_v1',snapshot_id,source_id,child_id,day,recipient_id],
                      ensure_ascii=False,separators=(',', ':')).encode()


def _key(key):
    if not isinstance(key, bytes) or len(key) not in (16,24,32):
        raise ValueError('LOCAL_ENCRYPTION_KEY_UNAVAILABLE')
    return key


def _consent_generation(c, child_id, recipient_id):
    consent = c.execute('SELECT consented_at FROM report_delivery_recipient WHERE child_id=? AND recipient_id=?', (child_id,recipient_id)).fetchone()
    if not consent or not isinstance(consent['consented_at'], str) or not consent['consented_at']:
        raise ValueError('RECIPIENT_AUTHORITY_UNCONFIRMED')
    revoked = c.execute("""SELECT audit_id FROM audit_event WHERE child_id=? AND action='report_delivery_recipient_revoked'
        AND json_extract(summary_json,'$.recipient_id')=? ORDER BY rowid DESC LIMIT 1""", (child_id,recipient_id)).fetchone()
    return {'consented_at': consent['consented_at'], 'last_revoke_id': revoked[0] if revoked else None}


def _decrypt(row, key):
    try:
        raw = bytes(row['encrypted_payload'])
        if not raw.startswith(b'HSR1') or len(raw) < 32: raise ValueError()
        body = AESGCM(_key(key)).decrypt(raw[4:16], raw[16:],
            _aad(row['snapshot_id'],row['source_id'],row['child_id'],row['day'],row['recipient_id']))
        if sha256(body).hexdigest() != row['payload_sha256']: raise ValueError()
        decoded = json.loads(body)
        if (not isinstance(decoded, dict) or decoded.get('version') != 1
                or decoded.get('generated_at') != row['generated_at']): raise ValueError()
        return decoded
    except Exception:
        raise ValueError('FROZEN_SNAPSHOT_UNREADABLE') from None


def _detailed_content(c, child_id, projection):
    """Keep more than the UI's 1200-character preview, inside encryption only."""
    pages, questions = {}, {}
    for page in projection['visual']['pages']:
        for observation in page['observations']:
            artifact_id = observation['artifact_id']
            row = c.execute('SELECT extracted_text,details_json FROM capture_artifact WHERE artifact_id=? AND child_id=?',
                            (artifact_id,child_id)).fetchone()
            if not row or not isinstance(row['extracted_text'], str): raise ValueError('FROZEN_SOURCE_MISSING')
            if page.get('source') == 'cloud_page':
                from .daily_visual_evidence import cloud_page_candidate
                text = cloud_page_candidate(row['details_json'])
                if not text: raise ValueError('FROZEN_SOURCE_MISSING')
            else:
                text = '\n'.join(line.strip() for line in row['extracted_text'].splitlines() if line.strip())
            pages[artifact_id] = {'artifact_id':artifact_id,'text':text[:12000],'text_truncated':len(text)>12000}
    for question in projection['visual']['questions']:
        for observation in question['observations']:
            question_id = observation['question_id']
            row = c.execute('SELECT canonical_text FROM observed_question WHERE question_id=? AND child_id=?',
                            (question_id,child_id)).fetchone()
            if not row or not isinstance(row['canonical_text'], str): raise ValueError('FROZEN_SOURCE_MISSING')
            text = '\n'.join(line.strip() for line in row['canonical_text'].splitlines() if line.strip())
            questions[question_id] = {'question_id':question_id,'text':text[:12000],'text_truncated':len(text)>12000}
    return {'pages':list(pages.values()),'questions':list(questions.values())}


class DailyReportFrozenSnapshotService:
    def __init__(self, database, encryption_key):
        self.database = database
        self.encryption_key = encryption_key

    def freeze(self, child_id, day, recipient_id, source_id):
        from .daily_report_frozen_dependencies import build, valid
        key = _key(self.encryption_key)
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                address = _authority(c,child_id,day,recipient_id,key)
                consent = _consent_generation(c,child_id,recipient_id)
                if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
                    raise ValueError('DELETION_PENDING')
                source = c.execute('''SELECT s.* FROM daily_observation_report_source s
                    JOIN daily_observation_report_outbox o ON o.source_id=s.source_id
                     AND o.child_id=s.child_id AND o.day=s.day
                    WHERE s.source_id=? AND s.child_id=? AND s.day=? AND o.recipient_id=?
                      AND o.address_identity=? AND o.status='prepared' ''',
                    (source_id,child_id,day,recipient_id,address)).fetchone()
                if not source: raise ValueError('PREPARED_SOURCE_NOT_FOUND')
                existing = c.execute('SELECT * FROM daily_observation_report_snapshot WHERE source_id=?', (source_id,)).fetchone()
                if existing:
                    body = _decrypt(existing,key)
                    if (existing['child_id'] != child_id or existing['day'] != day or existing['recipient_id'] != recipient_id
                            or body.get('address_identity') != address or body.get('consent_generation') != consent
                            or not valid(c,child_id,body.get('dependencies'))):
                        raise ValueError('FROZEN_SOURCE_CHANGED')
                    c.commit()
                    return {'snapshot_id':existing['snapshot_id'],'status':'frozen_local_only','external_delivery_permitted':False}
                projection = _projection(c,child_id,day)
                if _source_digest(projection) != (source['fingerprint'], source['source_ids_json']):
                    raise ValueError('PREPARED_SOURCE_CHANGED')
                dependencies = build(c,child_id,projection)
                details = _detailed_content(c,child_id,projection)
                generated = datetime.now(timezone.utc).isoformat()
                snapshot_id = str(uuid4())
                body = json.dumps({'version':1,'generated_at':generated,'address_identity':address,'consent_generation':consent,
                    'projection':projection,'details':details,'dependencies':dependencies},
                    ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()
                if len(body) > 1024*1024: raise ValueError('FROZEN_SNAPSHOT_TOO_LARGE')
                nonce = os.urandom(12)
                encrypted = b'HSR1' + nonce + AESGCM(key).encrypt(nonce,body,_aad(snapshot_id,source_id,child_id,day,recipient_id))
                c.execute('''INSERT INTO daily_observation_report_snapshot(snapshot_id,source_id,child_id,day,recipient_id,generated_at,payload_sha256,encrypted_payload,status)
                    VALUES(?,?,?,?,?,?,?,?,'frozen_local')''', (snapshot_id,source_id,child_id,day,recipient_id,generated,sha256(body).hexdigest(),encrypted))
                c.commit()
                return {'snapshot_id':snapshot_id,'status':'frozen_local_only','external_delivery_permitted':False}
            except BaseException:
                c.rollback()
                raise

    def inspect(self, child_id, day, recipient_id, snapshot_id):
        from .daily_report_frozen_dependencies import valid
        key = _key(self.encryption_key)
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN')
            try:
                address = _authority(c,child_id,day,recipient_id,key)
                consent = _consent_generation(c,child_id,recipient_id)
                if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
                    raise ValueError('DELETION_PENDING')
                row = c.execute('''SELECT f.* FROM daily_observation_report_snapshot f
                    JOIN daily_observation_report_outbox o ON o.source_id=f.source_id
                        AND o.child_id=f.child_id AND o.day=f.day AND o.recipient_id=f.recipient_id
                    WHERE f.snapshot_id=? AND f.child_id=? AND f.day=? AND f.recipient_id=?
                      AND o.address_identity=? AND o.status='prepared' AND f.status='frozen_local' ''',
                    (snapshot_id,child_id,day,recipient_id,address)).fetchone()
                if not row: raise ValueError('FROZEN_SNAPSHOT_NOT_FOUND')
                body = _decrypt(row,key)
                if body.get('address_identity') != address or body.get('consent_generation') != consent:
                    raise ValueError('FROZEN_RECIPIENT_CHANGED')
                return {'current':bool(valid(c,child_id,body.get('dependencies'))),
                        'generated_at':row['generated_at'],'external_delivery_permitted':False}
            finally:
                c.rollback()

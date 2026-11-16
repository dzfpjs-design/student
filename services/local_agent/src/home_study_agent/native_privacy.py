"""Native destructive permits: authority fences, never a second result ledger."""
import hashlib
import hmac
import json
import re
from uuid import uuid4


class NativePrivacyConflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def command_hash(path, body):
    return hashlib.sha256(canonical([path, body]).encode()).hexdigest()


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS controlled_launch_epoch(
        child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
        epoch TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS native_privacy_permit(
        nonce TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('claimed','finished','failed','cancelled')))''')


def epoch(c, session_id):
    ensure_schema(c)
    owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    if not owner:
        raise NativePrivacyConflict('NATIVE_LAUNCH_OWNER_CHANGED')
    c.execute('INSERT OR IGNORE INTO controlled_launch_epoch VALUES(?,?)', (owner[0], str(uuid4())))
    return {'childId': owner[0], 'sessionId': session_id,
            'epoch': c.execute('SELECT epoch FROM controlled_launch_epoch WHERE child_id=?', (owner[0],)).fetchone()[0]}


def check_epoch(c, session_id, value, intent_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_epoch'").fetchone():
        return
    row = c.execute('''SELECT e.epoch FROM controlled_launch_epoch e JOIN daily_session s
                       ON s.child_id=e.child_id WHERE s.session_id=?''', (session_id,)).fetchone()
    if row and (not intent_id or value != row[0]):
        raise NativePrivacyConflict('NATIVE_LAUNCH_EPOCH_CHANGED')


def rotate_child(c, child_id):
    ensure_schema(c)
    c.execute('UPDATE controlled_launch_epoch SET epoch=? WHERE child_id=?', (str(uuid4()), child_id))


def preserve_live_authority(live, staged):
    """Restore cannot import consumed permits or an old launch epoch as authority."""
    live_permits = (live.execute('SELECT * FROM native_privacy_permit').fetchall()
                    if live.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone() else [])
    live_children = (live.execute('SELECT child_id FROM controlled_launch_epoch').fetchall()
                     if live.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_epoch'").fetchone() else [])
    ensure_schema(staged)
    children = {r[0] for r in live_children} | {r[0] for r in staged.execute('SELECT child_id FROM controlled_launch_epoch')}
    staged.execute('DELETE FROM native_privacy_permit')
    staged.executemany('INSERT INTO native_privacy_permit VALUES(?,?,?)', [tuple(r) for r in live_permits])
    staged.execute('DELETE FROM controlled_launch_epoch')
    for child in children:
        if staged.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child,)).fetchone():
            staged.execute('INSERT INTO controlled_launch_epoch VALUES(?,?)', (child, str(uuid4())))


FIELDS = {'nonce', 'request_id', 'expected_generation', 'kind', 'child_id', 'scope',
          'command_sha256', 'cleanup_manifest_sha256'}


def verify(value, key):
    if not isinstance(value, dict) or set(value) != FIELDS | {'signature'}:
        raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_REQUIRED')
    metadata = {k: value[k] for k in FIELDS}
    for k in ('nonce', 'request_id', 'expected_generation'):
        if not isinstance(metadata[k], str) or not 1 <= len(metadata[k]) <= 128:
            raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_INVALID')
    for k in ('command_sha256', 'cleanup_manifest_sha256'):
        if not isinstance(metadata[k], str) or not re.fullmatch('[a-f0-9]{64}', metadata[k]):
            raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_INVALID')
    if metadata['kind'] not in {'delete', 'restore', 'portable_restore'}:
        raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_INVALID')
    if any(v is not None and (not isinstance(v, str) or not 1 <= len(v) <= 128)
           for v in (metadata['child_id'], metadata['scope'])):
        raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_INVALID')
    if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
        raise NativePrivacyConflict('NATIVE_PRIVACY_AUTHORITY_UNAVAILABLE')
    signature = value['signature']
    expected = hmac.new(bytes.fromhex(key), canonical(['native_privacy_permit_v1', metadata]).encode(), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not re.fullmatch('[a-f0-9]{64}', signature) or not hmac.compare_digest(signature, expected):
        raise NativePrivacyConflict('NATIVE_PRIVACY_PROOF_INVALID')
    return metadata


def read(c, metadata):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone():
        return {**metadata, 'status': 'missing'}
    row = c.execute('SELECT metadata_json,status FROM native_privacy_permit WHERE nonce=?', (metadata['nonce'],)).fetchone()
    if row and row[0] != canonical(metadata):
        raise NativePrivacyConflict('NATIVE_PRIVACY_SCOPE_CHANGED')
    return {**metadata, 'status': row[1] if row else 'missing'}


def claim(c, metadata, path, body, kind, child_id=None, scope=None):
    if not c.in_transaction:
        raise NativePrivacyConflict('NATIVE_PRIVACY_TRANSACTION_REQUIRED')
    ensure_schema(c)
    if (metadata['command_sha256'] != command_hash(path, body) or metadata['kind'] != kind
            or metadata['child_id'] != child_id or metadata['scope'] != scope):
        raise NativePrivacyConflict('NATIVE_PRIVACY_SCOPE_CHANGED')
    current = c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]
    if metadata['expected_generation'] != current:
        raise NativePrivacyConflict('NATIVE_PRIVACY_GENERATION_CHANGED')
    operation = body.get('operation')
    if operation and (operation['request_id'] != metadata['request_id']
                      or operation['expected_generation'] != current
                      or operation['cleanup_manifest_sha256'] != metadata['cleanup_manifest_sha256']):
        raise NativePrivacyConflict('NATIVE_PRIVACY_SCOPE_CHANGED')
    if read(c, metadata)['status'] != 'missing':
        raise NativePrivacyConflict('NATIVE_PRIVACY_PERMIT_CONSUMED')
    for table in ('controlled_content_session','controlled_game_session'):
        if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
            active=c.execute(f"SELECT 1 FROM {table} WHERE status='active'" + (' AND child_id=?' if child_id else '') + ' LIMIT 1',
                             (child_id,) if child_id else ()).fetchone()
            if active:
                raise NativePrivacyConflict('NATIVE_PRIVACY_RESOURCE_UNSETTLED')
    c.execute('INSERT INTO native_privacy_permit VALUES(?,?,?)', (metadata['nonce'], canonical(metadata), 'claimed'))


def finish(c, metadata, status):
    if status not in {'finished', 'failed'}:
        raise NativePrivacyConflict('NATIVE_PRIVACY_STATUS_INVALID')
    read(c, metadata)
    c.execute("UPDATE native_privacy_permit SET status=? WHERE nonce=? AND status='claimed'", (status, metadata['nonce']))


def fence(c, metadata):
    """Caller holds external_effect_guard: no original destructive call can be active."""
    ensure_schema(c)
    state = read(c, metadata)['status']
    if state == 'missing':
        c.execute('INSERT INTO native_privacy_permit VALUES(?,?,?)', (metadata['nonce'], canonical(metadata), 'cancelled'))
    elif state == 'claimed':
        finish(c, metadata, 'failed')
    # Permit cancellation also gives the existing UI an explicit unapplied receipt
    # when the original parent POST never reached reserve(). Never erase a real result.
    prior = c.execute('SELECT 1 FROM privacy_operation_intent WHERE request_id=?', (metadata['request_id'],)).fetchone()
    if not prior:
        c.execute('''INSERT INTO privacy_operation_intent(request_id,kind,child_id,scope,request_sha256,
          cleanup_manifest_sha256,origin_generation,state) VALUES(?,?,?,?,?,?,?,'failed')''',
          (metadata['request_id'], metadata['kind'], metadata['child_id'], metadata['scope'],
           'native-cancelled:' + metadata['command_sha256'], metadata['cleanup_manifest_sha256'], metadata['expected_generation']))
    else:
        c.execute("UPDATE privacy_operation_intent SET state='failed' WHERE request_id=? AND state='processing' AND reference_id IS NULL",
                  (metadata['request_id'],))
    return read(c, metadata)


def assert_resource_permit(c, child_id, request_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_epoch'").fetchone():
        return
    managed = c.execute('SELECT 1 FROM controlled_launch_epoch' + (' WHERE child_id=?' if child_id else '') + ' LIMIT 1',
                        (child_id,) if child_id else ()).fetchone()
    if not managed:
        return
    for row in c.execute("SELECT metadata_json FROM native_privacy_permit WHERE status='claimed'"):
        metadata = json.loads(row[0])
        if metadata['request_id'] == request_id and metadata['child_id'] == child_id:
            return
    raise NativePrivacyConflict('NATIVE_PRIVACY_PREFLIGHT_REQUIRED')

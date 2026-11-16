"""Purpose-signed local capture read. This is not cloud or replay authority."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from .agent_runtime import AgentRuntimeStore
from .privacy_operation_receipt import generation
from .voice_capture_binding import read_realtime_admission_receipt, resolve_voice_capture, VoiceCaptureBindingError

SCOPE_FIELDS = ('session_id', 'child_id', 'client_action_id', 'voice_input_id', 'connection_id', 'storage_generation')
FIELDS = SCOPE_FIELDS + ('issued_at',)
DOMAIN = 'native_realtime_input_source_v1'


class RealtimeInputSourceConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class RealtimeInputSource:
    def __init__(self, database, key, *, page_source_store=None, clock=None):
        self.database, self.key, self.page_source_store = database, key, page_source_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _verify(self, value):
        if not isinstance(value, dict) or set(value) != set(FIELDS) | {'signature'}:
            raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_INVALID')
        if not isinstance(self.key, str) or not re.fullmatch('[a-f0-9]{64}', self.key):
            raise RealtimeInputSourceConflict('REALTIME_INPUT_AUTHORITY_UNAVAILABLE')
        if any(not isinstance(value[f], str) or not re.fullmatch('[a-zA-Z0-9_-]{8,128}', value[f]) for f in SCOPE_FIELDS):
            raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_INVALID')
        try:
            issued = datetime.fromisoformat(value['issued_at'].replace('Z', '+00:00'))
            if issued.tzinfo is None or not -5 <= (self.clock() - issued).total_seconds() <= 30:
                raise ValueError('expired')
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_EXPIRED') from None
        expected = hmac.new(bytes.fromhex(self.key), _json([DOMAIN] + [value[f] for f in FIELDS]).encode(), hashlib.sha256).hexdigest()
        if (not isinstance(value['signature'], str) or not re.fullmatch('[a-f0-9]{64}', value['signature'])
                or not hmac.compare_digest(value['signature'], expected)):
            raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_INVALID')
        return {f: value[f] for f in SCOPE_FIELDS}

    def read(self, value):
        scope = self._verify(value)
        with self.database.connect() as c:
            c.execute('BEGIN')
            if scope['storage_generation'] != generation(c):
                raise RealtimeInputSourceConflict('REALTIME_INPUT_STORAGE_CHANGED')
            # Reading original local evidence does not require or grant cloud
            # permission; protected controls must still reach the local Runtime.
            owner = c.execute('''SELECT d.status AS session_status,c.status AS child_status
                FROM daily_session d JOIN child_profile c ON d.child_id=c.child_id
                WHERE d.session_id=? AND d.child_id=?''', (scope['session_id'], scope['child_id'])).fetchone()
            if not owner or owner['session_status'] != 'active' or owner['child_status'] != 'active':
                raise RealtimeInputSourceConflict('REALTIME_INPUT_SOURCE_NOT_CURRENT')
            try:
                receipt = read_realtime_admission_receipt(self.database, scope['session_id'], scope['client_action_id'], connection=c)
                if (receipt['child_id'] != scope['child_id'] or receipt['status'] != 'completed'
                        or receipt['voice_input_id'] != scope['voice_input_id']):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_MISSING')
                row = c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (scope['voice_input_id'],)).fetchone()
                text = row['transcript']
                if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
                capture = resolve_voice_capture(c, session_id=scope['session_id'], child_id=scope['child_id'],
                    voice_input_id=scope['voice_input_id'], transcript=text, page_source_store=self.page_source_store)
                if capture.get('source_kind') != 'realtime_transcript' or capture.get('raw_audio_available'):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_INVALID')
            except (VoiceCaptureBindingError, TypeError, KeyError):
                raise RealtimeInputSourceConflict('REALTIME_INPUT_SOURCE_CHANGED') from None
            digest = hashlib.sha256(_json([scope, text, AgentRuntimeStore.voice_capture_snapshot(capture)]).encode()).hexdigest()
            action = 'realtime-' + scope['voice_input_id']
            dispatched = c.execute('SELECT 1 FROM learning_input_arrival WHERE session_id=? AND client_action_id=?',
                                   (scope['session_id'], action)).fetchone()
            latest = c.execute('SELECT client_action_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                               (scope['session_id'],)).fetchone()
            # A new capture has not reached Runtime arrival yet. Local evidence
            # stays readable after supersession, without permitting its replay.
            arrival = 'not_dispatched' if not dispatched else 'current' if latest and latest[0] == action else 'superseded'
            return {'scope': scope, 'status': 'completed', 'transcript': text, 'source_hash': digest,
                    'arrival_status': arrival,
                    'source_kind': 'realtime_transcript', 'onset_provenance': capture.get('onset_provenance', 'unknown'),
                    'local_only': True, 'cloud_transmission_allowed': False, 'replay_allowed': False, 'mastery_update_allowed': False}

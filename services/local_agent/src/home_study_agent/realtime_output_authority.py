"""Runtime-owned output permission, not a new speech or semantic provider."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from .cloud_privacy import sensitive_categories, sanitize_cloud_text
from .child_safety import classify_child_safety
from .voice_scope import scope_valid, text_consent_version
from .visual_teaching import local_speech_required
from .privacy_operation_receipt import generation

FIELDS = ('operation', 'session_id', 'child_id', 'agent_turn_id', 'connection_id',
          'call_id', 'voice', 'permit_id', 'storage_generation', 'response_id', 'issued_at')
SCOPE_FIELDS = ('session_id', 'child_id', 'agent_turn_id', 'connection_id',
                'call_id', 'voice', 'permit_id', 'storage_generation')
DOMAIN = 'native_realtime_output_v1'


class RealtimeOutputConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class RealtimeOutputAuthority:
    def __init__(self, database, key, *, page_source_store=None, clock=None, approved_model_routes=()):
        self.database, self.key, self.page_source_store = database, key, page_source_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.approved_model_routes = frozenset(approved_model_routes)

    def _verify(self, value):
        if not isinstance(value, dict) or set(value) not in (set(FIELDS) | {'signature'}, set(FIELDS) | {'signature', 'generation_evidence'}):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        if not isinstance(self.key, str) or not re.fullmatch('[a-f0-9]{64}', self.key):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_AUTHORITY_UNAVAILABLE')
        for field in FIELDS[1:6] + ('permit_id', 'storage_generation'):
            if not isinstance(value[field], str) or not re.fullmatch('[a-zA-Z0-9_-]{8,128}', value[field]):
                raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        if value['operation'] not in {'reserve', 'bind', 'inspect', 'retire', 'generated', 'started', 'rendered', 'interrupted', 'unconfirmed'}:
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        if not isinstance(value['voice'], str) or not re.fullmatch('[a-zA-Z0-9._:-]{2,180}', value['voice']):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        response_id = value['response_id']
        if not isinstance(response_id, str) or response_id and not re.fullmatch('[a-zA-Z0-9_-]{8,128}', response_id):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        if value['operation'] in {'bind', 'generated', 'started', 'rendered', 'interrupted', 'unconfirmed'} and not response_id or value['operation'] == 'reserve' and response_id:
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        try:
            issued = datetime.fromisoformat(value['issued_at'].replace('Z', '+00:00'))
            age = (self.clock() - issued).total_seconds()
            if issued.tzinfo is None or not -5 <= age <= 30:
                raise ValueError('expired')
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_EXPIRED') from None
        evidence = value.get('generation_evidence')
        if value['operation'] == 'generated':
            if (not isinstance(evidence, dict) or set(evidence) != {'sample_count', 'pcm_bytes', 'frame_count', 'pcm_sha256', 'text_sha256', 'model', 'voice', 'supplier_session_id', 'supplier_tool_response_id', 'supplier_tool_call_id'}
                    or any(type(evidence[k]) is not int for k in ('sample_count', 'pcm_bytes', 'frame_count'))
                    or not 1 <= evidence['frame_count'] <= 4096 or not 0 < evidence['pcm_bytes'] <= 8000000
                    or evidence['sample_count'] * 2 != evidence['pcm_bytes']
                    or any(not isinstance(evidence[k], str) or not re.fullmatch('[a-f0-9]{64}', evidence[k]) for k in ('pcm_sha256', 'text_sha256'))
                    or any(not isinstance(evidence[k], str) or not re.fullmatch('[a-zA-Z0-9_-]{8,128}', evidence[k]) for k in ('supplier_session_id', 'supplier_tool_response_id', 'supplier_tool_call_id'))
                    or evidence['voice'] != value['voice'] or evidence['model'] != 'qwen3.8-omni-flash-realtime'):
                raise RealtimeOutputConflict('REALTIME_OUTPUT_GENERATION_EVIDENCE_INVALID')
        elif 'generation_evidence' in value:
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        expected = hmac.new(bytes.fromhex(self.key), _json([DOMAIN] + [value[f] for f in FIELDS] + ([evidence] if 'generation_evidence' in value else [])).encode(), hashlib.sha256).hexdigest()
        if (not isinstance(value['signature'], str) or not re.fullmatch('[a-f0-9]{64}', value['signature'])
                or not hmac.compare_digest(value['signature'], expected)):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
        return {**{field: value[field] for field in FIELDS}, **({'generation_evidence': evidence} if evidence else {})}

    def _source(self, c, command):
        row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND session_id=? AND child_id=?',
                        (command['agent_turn_id'], command['session_id'], command['child_id'])).fetchone()
        if not row or row['tool_status'] != 'completed' or not scope_valid(c, row['session_id'], row['child_id'], row['client_action_id']):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_SOURCE_NOT_CURRENT')
        try:
            action = json.loads(row['action_json'])
            text = row['response_text']
            if not isinstance(action, dict):
                raise ValueError('invalid action')
        except (ValueError, TypeError, AttributeError):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_SOURCE_INVALID') from None
        if any(field in action and type(action[field]) is not bool for field in ('local_only', 'cloud_transmission_allowed')):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_LOCAL_ONLY')
        if row['intent'] == 'memory_command' or local_speech_required(c, row['session_id'], text):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_LOCAL_ONLY')
        from .public_reply_projection import inspect as inspect_public_reply
        try:
            projection_hash = inspect_public_reply(c, row, self.approved_model_routes)
        except (ValueError, KeyError, TypeError):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_PUBLIC_SOURCE_INVALID') from None
        # Reuse the stored recall backstop from the existing TTS boundary, not a
        # caller's claim that a public turn contains no local material.
        recalls = c.execute("SELECT response_text FROM agent_turn WHERE session_id=? AND intent='memory_command'", (row['session_id'],)).fetchall()
        if any(text in r[0] or r[0] in text for r in recalls if r[0]):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_LOCAL_ONLY')
        if not text.strip() or len(text) > 1000 or sensitive_categories(text) or classify_child_safety(text):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_TEXT_NOT_ELIGIBLE')
        names = [item[0] for item in c.execute('SELECT nickname FROM child_profile') if item[0]]
        if sanitize_cloud_text(text, names).text != text:
            raise RealtimeOutputConflict('REALTIME_OUTPUT_TEXT_NOT_ELIGIBLE')
        if row['client_action_id'].startswith('realtime-'):
            from .canonical_voice_execution import inspect
            try:
                if not inspect(c, row['agent_turn_id'], page_source_store=self.page_source_store):
                    raise ValueError('not canonical')
            except ValueError:
                raise RealtimeOutputConflict('REALTIME_OUTPUT_SOURCE_CHANGED') from None
        return text, _hash([row['intent'], row['response_text'], row['action_json'], row['completed_at'], projection_hash]), text_consent_version(c)

    def _event(self, c, audit_id):
        row = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=? AND actor=?', (audit_id, DOMAIN)).fetchone()
        try:
            value = json.loads(row[0]) if row else None
            if row and (not isinstance(value, dict) or value.get('state') not in {'reserved', 'bound', 'retired', 'revoked'}
                        or not isinstance(value.get('scope'), dict)):
                raise ValueError('invalid receipt')
            return value
        except (ValueError, TypeError):
            raise RealtimeOutputConflict('REALTIME_OUTPUT_RECEIPT_INVALID') from None

    def _input_source(self, c, turn_id):
        row = c.execute('SELECT client_action_id,action_json FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
        action = json.loads(row['action_json'])
        return {'client_action_id': row['client_action_id'],
                'voice_input_id': action.get('voice_input_id') if row['client_action_id'].startswith('realtime-') else None}

    def _append(self, c, audit_id, command, state):
        c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
                  (audit_id, DOMAIN, 'realtime_output_' + state.get('delivery_status', state['state']), command['child_id'], _json(state), self.clock().isoformat()))

    def execute(self, value):
        command = self._verify(value)
        # One original output opportunity per durable Runtime turn, even when a
        # renderer supplies a new connection/call/nonce after losing the receipt.
        audit_id = 'realtime-output-' + _hash(command['agent_turn_id'])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            reserved = self._event(c, audit_id)
            bound = self._event(c, audit_id + '-bind')
            retired = self._event(c, audit_id + '-retire')
            settled = self._event(c, audit_id + '-settled')
            operation = command['operation']
            scope = {f: command[f] for f in SCOPE_FIELDS}
            epoch = hmac.new(bytes.fromhex(self.key), b'native_realtime_output_epoch_v1', hashlib.sha256).hexdigest()
            if operation == 'reserve':
                if command['storage_generation'] != generation(c):
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_STORAGE_CHANGED')
                if reserved:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_ALREADY_RESERVED')
                text, source_hash, consent = self._source(c, command)
                state = {'state': 'reserved', 'scope': scope, 'source_hash': source_hash, 'consent': consent,
                         'expires_at': (self.clock() + timedelta(seconds=120)).isoformat(), 'epoch': epoch}
                input_source = self._input_source(c, command['agent_turn_id'])
                self._append(c, audit_id, command, state)
                c.commit()
                return {'state': 'reserved', 'scope': scope, 'response_text': text, 'cloud_transmission_allowed': True,
                        'input_source': input_source,
                        'local_only': False, 'generation_completed': False, 'rendered_completed': False}
            if not reserved or reserved.get('scope') != scope:
                raise RealtimeOutputConflict('REALTIME_OUTPUT_SCOPE_CHANGED')
            if bound and command['response_id'] and bound.get('response_id') != command['response_id']:
                raise RealtimeOutputConflict('REALTIME_OUTPUT_SCOPE_CHANGED')
            if operation == 'retire':
                if not retired:
                    self._append(c, audit_id + '-retire', command, {'state': 'retired', 'scope': scope})
                    c.commit()
                return {'state': 'retired', 'scope': scope, 'dispatch_allowed': False}
            if retired:
                return {'state': retired['state'], 'scope': scope, 'dispatch_allowed': False}
            if settled:
                generated_receipt = self._event(c, audit_id + '-generated')
                return {'state': 'retired', 'scope': scope, 'dispatch_allowed': False,
                        'delivery_status': settled['delivery_status'], 'rendered_completed': settled['delivery_status'] == 'rendered',
                        'generation_completed': bool(generated_receipt),
                        'generation_evidence': generated_receipt.get('generation_evidence') if generated_receipt else None}
            try:
                source_text, source_hash, consent = self._source(c, command)
                fresh = (command['storage_generation'] == generation(c)
                         and reserved.get('epoch') == epoch and reserved.get('source_hash') == source_hash
                         and reserved.get('consent') == (list(consent) if consent else None)
                         and self.clock() <= datetime.fromisoformat(reserved['expires_at']))
            except (RealtimeOutputConflict, ValueError, TypeError, KeyError):
                fresh = False
            if not fresh:
                self._append(c, audit_id + '-retire', command, {'state': 'revoked', 'scope': scope})
                c.commit()
                return {'state': 'revoked', 'scope': scope, 'dispatch_allowed': False}
            if operation == 'bind':
                if bound:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_ALREADY_BOUND')
                bound = {'state': 'bound', 'scope': scope, 'response_id': command['response_id']}
                self._append(c, audit_id + '-bind', command, bound)
                c.commit()
                return {**bound, 'dispatch_allowed': True, 'generation_completed': False, 'rendered_completed': False}
            generated = self._event(c, audit_id + '-generated')
            started = self._event(c, audit_id + '-started')
            if operation in {'generated', 'started', 'rendered', 'interrupted', 'unconfirmed'}:
                if not bound or command['response_id'] != bound['response_id']:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_DELIVERY_NOT_BOUND')
                if operation in {'started', 'rendered'} and not generated or operation == 'rendered' and not started:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_DELIVERY_ORDER_INVALID')
                if operation == 'generated' and command['generation_evidence']['text_sha256'] != hashlib.sha256(source_text.encode()).hexdigest():
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_GENERATION_SOURCE_CHANGED')
                suffix = operation if operation in {'generated', 'started'} else 'settled'
                receipt = self._event(c, audit_id + '-' + suffix)
                if operation == 'generated' and receipt and receipt.get('generation_evidence') != command['generation_evidence']:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_GENERATION_SOURCE_CHANGED')
                if not receipt:
                    receipt = {'state': 'bound', 'scope': scope, 'response_id': bound['response_id'], 'delivery_status': operation}
                    if operation == 'generated': receipt['generation_evidence'] = command['generation_evidence']
                    self._append(c, audit_id + '-' + suffix, command, receipt)
                    c.commit()
                return {**receipt, 'dispatch_allowed': False, 'generation_completed': bool(generated or operation == 'generated'),
                        'rendered_completed': operation == 'rendered'}
            # Readback is never permission to resend function_call_output or
            # response.create. Only the first successful bind can dispatch.
            return {'state': 'bound' if bound else 'reserved', 'scope': scope,
                    'response_id': bound['response_id'] if bound else None, 'dispatch_allowed': False,
                    'delivery_status': 'started' if started else 'generated' if generated else 'unknown',
                    'generation_completed': bool(generated), 'rendered_completed': False}

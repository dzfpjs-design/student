"""One durable Runtime handoff per source, not another learning/result ledger."""
from hashlib import sha256
import json

from .repository import UnknownSessionError, utc_now
from uuid import uuid4
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


ACTION = 'agent_request_handoff_claimed'
RETRY = 'agent_handoff_local_retry_available'
USED = 'agent_handoff_local_retry_consumed'
REJECTED = 'agent_handoff_terminal_rejected'
# Only failures of an immutable arrival are terminal. Busy, consent changes,
# network errors and uncertain model work must not become cached rejection.
TERMINAL_CODES = frozenset({'ANSWER_WRITING_DAILY_SOURCE_UNAVAILABLE'})


class LocalHandoffDispatchDenied(BaseException):
    """Internal cancellation signal; model fallback must not swallow it."""


@dataclass
class ExecutionAuthority:
    allow_external: bool
    external_started: bool = False


_authority = ContextVar('agent_handoff_execution', default=None)
_voice_interaction_admission = ContextVar('voice_interaction_handoff_admission', default=None)


@contextmanager
def voice_interaction_handoff_admission(authorize):
    """Keep an HTTP proof attached to the same locked source claim."""
    token = _voice_interaction_admission.set(authorize)
    try:
        yield
    finally:
        _voice_interaction_admission.reset(token)


@contextmanager
def execution_scope(allow_external):
    inherited = _authority.get()
    authority = ExecutionAuthority(allow_external and (inherited is None or inherited.allow_external))
    token = _authority.set(authority)
    try:
        yield authority
    finally:
        if inherited is not None:
            inherited.external_started |= authority.external_started
        _authority.reset(token)


def authorize_external_dispatch():
    authority = _authority.get()
    if authority is not None:
        if not authority.allow_external:
            raise LocalHandoffDispatchDenied()
        authority.external_started = True


class OwnedModelGateway:
    """Apply the same local boundary to production and explicit test adapters."""
    def __init__(self, gateway):
        self.gateway = gateway

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    async def complete(self, request):
        authorize_external_dispatch()
        return await self.gateway.complete(request)

    async def complete_on_provider(self, provider, request):
        authorize_external_dispatch()
        return await self.gateway.complete_on_provider(provider, request)

    async def complete_on_fallback(self, request):
        authorize_external_dispatch()
        return await self.gateway.complete_on_fallback(request)


def _identities(action_id, voice_input_id):
    sources = [['action', action_id]] + ([['voice_input', voice_input_id]] if voice_input_id else [])
    return ['agent-handoff:' + sha256(json.dumps(source).encode()).hexdigest() for source in sources]


def voice_interaction_input_baseline(c, session_id, child_id):
    """Capture opaque input positions while the original voice claim is locked."""
    value = {}
    for table, key in [('learning_input_arrival', 'arrival_rowid'), ('agent_turn', 'turn_rowid')]:
        present = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        value[key] = c.execute(f'SELECT coalesce(max(rowid),0) FROM {table} WHERE session_id=? AND child_id=?',
            (session_id, child_id)).fetchone()[0] if present else 0
    value['claim_rowid'] = c.execute('SELECT coalesce(max(rowid),0) FROM audit_event WHERE action=? AND child_id=? '
        "AND json_valid(summary_json) AND json_extract(summary_json,'$.session_id')=?",
        (ACTION, child_id, session_id)).fetchone()[0]
    return value


def _fingerprint(session_id, action_id, transcript, voice_input_id, playback_overlap_id,
                 pending_evaluation_turn_id, local_recovery_only):
    return sha256(json.dumps([
        session_id, action_id, transcript.strip(), voice_input_id, playback_overlap_id,
        pending_evaluation_turn_id, local_recovery_only,
    ], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read_unsent_authority(c, session_id, child_id, action_id, transcript, voice_input_id,
                          pending_evaluation_turn_id=None):
    """A local preview; claim() must recheck and consume permission atomically."""
    identities = _identities(action_id, voice_input_id)
    fingerprint = _fingerprint(session_id, action_id, transcript, voice_input_id, None,
                               pending_evaluation_turn_id, False)
    found = False
    for identity in identities:
        row = c.execute('SELECT * FROM audit_event WHERE audit_id=?', (identity,)).fetchone()
        if not row:
            continue
        found = True
        try:
            saved = json.loads(row['summary_json'])
        except (TypeError, ValueError):
            return False
        if (row['action'] != ACTION or row['child_id'] != child_id or not isinstance(saved, dict)
                or saved.get('session_id') != session_id or saved.get('fingerprint') != fingerprint):
            return False
    if c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',
                 ('agent-handoff-rejection:' + identities[0],)).fetchone():
        return False
    if not found:
        # A pre-fence arrival is not proof that a model was never called.
        return not c.execute('SELECT 1 FROM learning_input_arrival WHERE client_action_id=?',
                             (action_id,)).fetchone()
    for retry in c.execute('SELECT audit_id,summary_json FROM audit_event WHERE action=? AND child_id=?', (RETRY, child_id)):
        try:
            evidence = json.loads(retry['summary_json'])
        except (TypeError, ValueError):
            continue
        if (evidence == {'session_id': session_id, 'claims': identities}
                and not c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',
                                  ('agent-handoff-local-retry-used:' + retry['audit_id'],)).fetchone()):
            return True
    return False


def permit_local_retry(database, session_id, action_id, voice_input_id):
    with database.connect() as c:
        child = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not child:
            raise UnknownSessionError(session_id)
        c.execute("INSERT INTO audit_event VALUES (?,'system',?,?,?,?)", (
            'agent-handoff-local-retry:' + str(uuid4()), RETRY, child[0],
            json.dumps({'session_id': session_id, 'claims': _identities(action_id, voice_input_id)}, separators=(',', ':')),
            utc_now()))
        c.commit()


def terminal_rejection(database, session_id, action_id, voice_input_id, *, record_code=None):
    identity = 'agent-handoff-rejection:' + _identities(action_id, voice_input_id)[0]
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not owner:
            raise UnknownSessionError(session_id)
        saved = c.execute('SELECT * FROM audit_event WHERE audit_id=?', (identity,)).fetchone()
        binding = {'session_id': session_id, 'claims': _identities(action_id, voice_input_id)}
        if saved:
            try:
                value = json.loads(saved['summary_json'])
            except (TypeError, ValueError):
                value = None
            if (saved['action'] != REJECTED or saved['child_id'] != owner[0]
                    or not isinstance(value, dict) or value.get('code') not in TERMINAL_CODES
                    or {k:v for k,v in value.items() if k != 'code'} != binding
                    or record_code is not None and record_code != value['code']):
                raise AgentRequestOwnershipError('AGENT_HANDOFF_SOURCE_CONFLICT')
            return value['code']
        if record_code is not None:
            if record_code not in TERMINAL_CODES:
                raise ValueError('Handoff rejection is not terminal.')
            c.execute("INSERT INTO audit_event VALUES (?,'system',?,?,?,?)", (identity, REJECTED,
                owner[0], json.dumps({**binding, 'code': record_code}, separators=(',', ':')), utc_now()))
            c.commit()
        return record_code


class AgentRequestOwnershipError(ValueError):
    pass


def claim(database, session_id, action_id, transcript, *, voice_input_id=None,
          playback_overlap_id=None, pending_evaluation_turn_id=None, local_recovery_only=False,
          allow_local_resume=False, require_unsent_resume=False):
    identities = _identities(action_id, voice_input_id)
    fingerprint = _fingerprint(session_id, action_id, transcript, voice_input_id, playback_overlap_id,
                               pending_evaluation_turn_id, local_recovery_only)
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not owner:
            raise UnknownSessionError(session_id)
        authorize = _voice_interaction_admission.get()
        if authorize is not None:
            try:
                authorize(c)
            except AgentRequestOwnershipError:
                # Admission has not mutated anything yet. Preserve a scope
                # invalidation detected by the proof before denying dispatch.
                c.commit()
                raise
        elif action_id.startswith('voice-handoff-'):
            raise AgentRequestOwnershipError('AGENT_HANDOFF_SOURCE_CONFLICT')
        prior_found = False
        for identity in identities:
            prior = c.execute('SELECT child_id,summary_json,action FROM audit_event WHERE audit_id=?',
                              (identity,)).fetchone()
            if not prior:
                continue
            prior_found = True
            try:
                saved = json.loads(prior['summary_json'])
            except (TypeError, ValueError):
                saved = None
            if (prior['action'] != ACTION or prior['child_id'] != owner[0] or not isinstance(saved, dict)
                    or saved.get('fingerprint') != fingerprint or saved.get('session_id') != session_id):
                raise AgentRequestOwnershipError('AGENT_HANDOFF_SOURCE_CONFLICT')
        if require_unsent_resume:
            from .voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
            active = c.execute('SELECT status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            try:
                capture = resolve_voice_capture(c, session_id=session_id, child_id=owner[0],
                                               voice_input_id=voice_input_id, transcript=transcript)
            except VoiceCaptureBindingError:
                raise AgentRequestOwnershipError('AGENT_HANDOFF_SOURCE_CONFLICT') from None
            if (not active or active[0] != 'active' or capture.get('source_kind') != 'realtime_transcript'
                    or not read_unsent_authority(c, session_id, owner[0], action_id, transcript,
                                               voice_input_id, pending_evaluation_turn_id)):
                raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')
        # Existing turns remain the only result authority, including old records
        # predating this fence. The caller still validates payload and provenance.
        if c.execute('SELECT 1 FROM agent_turn WHERE client_action_id=?', (action_id,)).fetchone():
            if require_unsent_resume:
                raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')
            return True
        if prior_found:
            # The validated safety-only path has no external/model work and
            # persists its rescue/turn in one idempotent write transaction.
            if local_recovery_only:
                if require_unsent_resume:
                    raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')
                return False
            if allow_local_resume:
                for retry in c.execute('SELECT audit_id,summary_json FROM audit_event WHERE action=? AND child_id=? ORDER BY rowid DESC', (RETRY, owner[0])):
                    try:
                        evidence = json.loads(retry['summary_json'])
                    except (TypeError, ValueError):
                        continue
                    consumed = 'agent-handoff-local-retry-used:' + retry['audit_id']
                    if (evidence == {'session_id': session_id, 'claims': identities}
                            and not c.execute('SELECT 1 FROM audit_event WHERE audit_id=?', (consumed,)).fetchone()):
                        c.execute("INSERT INTO audit_event VALUES (?,'system',?,?,?,?)", (consumed, USED,
                            owner[0], retry['summary_json'], utc_now()))
                        c.commit()
                        return True
                if require_unsent_resume:
                    raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')
                return False
            # No clock/expiry implies permission to resend a possibly paid call.
            raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')
        for identity in identities:
            c.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES (?,'system',?,?,?,?)''', (identity, ACTION, owner[0], json.dumps({
                    'version': 1, 'session_id': session_id, 'fingerprint': fingerprint,
                }, separators=(',', ':')), utc_now()))
        c.commit()
        return True


def preserve_live_claims(live, staged):
    """A backup from before dispatch must not erase the consumed source fence."""
    if not live.execute("SELECT 1 FROM sqlite_master WHERE name='audit_event'").fetchone():
        return
    for row in live.execute('SELECT * FROM audit_event WHERE action IN (?,?,?,?)', (ACTION, RETRY, USED, REJECTED)):
        prior = staged.execute('SELECT * FROM audit_event WHERE audit_id=?', (row[0],)).fetchone()
        if prior and tuple(prior) != tuple(row):
            raise AgentRequestOwnershipError('AGENT_HANDOFF_RESTORE_CONFLICT')
        staged.execute('INSERT OR IGNORE INTO audit_event VALUES (?,?,?,?,?,?)', tuple(row))

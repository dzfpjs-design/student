"""Opaque, durable proof for the exact tutorial-to-Agent transition.

Original speech remains in cloud_voice_interaction. Audit pointers contain only
identities, fingerprints and scope versions. Reads never submit a command;
agent_request_ownership.claim consumes admission under its own write lock.
"""
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .agent_request_ownership import ACTION, _identities, read_unsent_authority, AgentRequestOwnershipError
from .agent_request_receipt import read_agent_request_receipt
from .cloud_voice_interaction import (
    VoiceInteractionConflictError, VoiceInteractionRequest, VoiceInteractionResponse, _valid_name,
)
from .repository import utc_now
from .voice_control_intent import VoiceControlIntent


PREPARED = 'voice_interaction_handoff_prepared'
INVALIDATED = 'voice_interaction_handoff_invalidated'


class VoiceInteractionHandoffRead(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    child_id: str = Field(min_length=2, max_length=120)


class VoiceInteractionHandoffPrepare(VoiceInteractionHandoffRead):
    original_action_key: str = Field(pattern=r'^voice-interaction:[0-9a-f]{64}$')


class DeferredVoiceOnboardingProgress(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    childId: str
    version: Literal[1] = 1
    step: Literal['deferred'] = 'deferred'
    updatedAt: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')


class HandoffAgentReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    session_id: str
    client_action_id: str
    status: Literal['recorded_completed', 'recorded_failed', 'interpreted', 'received', 'unconfirmed']
    response_text: str
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]
    replay_allowed: Literal[False]
    mastery_update_allowed: Literal[False]


class VoiceInteractionHandoffResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    session_id: str
    child_id: str
    status: Literal['none', 'needs_deferral', 'ready', 'received', 'interpreted',
                    'completed', 'failed', 'outcome_unknown', 'invalidated']
    handoff_id: Optional[str] = None
    agent_action_id: Optional[str] = None
    deferred_progress: Optional[DeferredVoiceOnboardingProgress] = None
    request: Optional[VoiceInteractionRequest] = None
    receipt: Optional[HandoffAgentReceipt] = None
    reason: Optional[str] = None


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _identity(source_key):
    return 'voice-interaction-handoff:' + _hash(source_key)


class VoiceInteractionHandoffService:
    def __init__(self, voice_service):
        self.voice = voice_service
        self.database = voice_service.database

    @staticmethod
    def _owner(c, session_id, child_id):
        session = c.execute('SELECT * FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not session or session['child_id'] != child_id:
            raise VoiceInteractionConflictError('VOICE_INTERACTION_RECOVERY_SCOPE_CHANGED')
        return session

    @staticmethod
    def _latest_source(c, session_id, child_id):
        return c.execute('SELECT rowid AS source_rowid,* FROM cloud_voice_interaction '
            'WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1', (session_id, child_id)).fetchone()

    @staticmethod
    def _latest_pointer(c, session_id, child_id):
        return c.execute('SELECT * FROM audit_event WHERE action=? AND child_id=? '
            "AND json_valid(summary_json) AND json_extract(summary_json,'$.session_id')=? ORDER BY rowid DESC LIMIT 1",
            (PREPARED, child_id, session_id)).fetchone()

    @staticmethod
    def _tombstone_key(handoff_id):
        return 'voice-interaction-handoff-invalidated:' + _hash(handoff_id)

    def _invalidate(self, c, session_id, child_id, handoff_id):
        c.execute('INSERT OR IGNORE INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) '
            "VALUES (?,'child',?,?,?,?)", (self._tombstone_key(handoff_id), INVALIDATED, child_id,
                json.dumps({'version': 1, 'session_id': session_id, 'handoff_id': handoff_id}), utc_now()))

    @staticmethod
    def _response(session_id, child_id, status, pointer=None, **kwargs):
        value = pointer or {}
        return VoiceInteractionHandoffResponse(session_id=session_id, child_id=child_id, status=status,
            handoff_id=value.get('handoff_id'), agent_action_id=value.get('agent_action_id'),
            deferred_progress=value.get('deferred_progress'), **kwargs)

    @staticmethod
    def _initial_onboarding(profile, payload):
        progress, wake, context = profile['progress'], profile['wake'], payload.context
        if progress and progress['step'] in {'completed', 'deferred'}:
            return False
        if context.phase == 'tutorial':
            return bool(progress and wake and wake['enabled'] and progress['step'] == context.step
                and wake['wakeWord'] == context.wake_word and not context.pending_name)
        return bool(not (wake and wake['enabled']) and context.step is None and not context.wake_word
            and (not context.pending_name if context.phase == 'naming'
                 else context.pending_name and _valid_name(context.pending_name)))

    def _source(self, c, session_id, child_id, source_key):
        row = self._latest_source(c, session_id, child_id)
        if not row or row['action_key'] != source_key or not row['request_json'] or not row['onboarding_scope_json']:
            raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
        payload = VoiceInteractionRequest.model_validate_json(row['request_json'])
        response = VoiceInteractionResponse.model_validate_json(row['response_json']) if row['response_json'] else None
        if (not response or response.source != 'cloud' or response.route != 'agent' or response.action != 'none'
                or not row['completed_at']):
            raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
        self.voice._assert_voice_input(c, session_id, child_id, payload)
        saved = json.loads(row['onboarding_scope_json'])
        if (not isinstance(saved, dict) or set(saved) != {'profile', 'consent_version'}
                or not self._initial_onboarding(saved['profile'], payload)):
            raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
        audit = c.execute("SELECT summary_json FROM audit_event WHERE audit_id=? AND child_id=? AND action='voice_interaction_completed'",
                          (source_key, child_id)).fetchone()
        proof = json.loads(audit[0]) if audit else {}
        baseline = proof.get('agent_handoff_baseline')
        if (proof.get('session_id') != session_id or proof.get('fingerprint') != row['fingerprint']
                or proof.get('status') != 'completed' or not isinstance(baseline, dict)
                or set(baseline) != {'arrival_rowid', 'turn_rowid', 'claim_rowid'}
                or any(type(value) is not int or value < 0 for value in baseline.values())):
            # Pre-boundary records retain ordinary voice recovery, but cannot
            # manufacture evidence that no different input intervened.
            raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
        return row, payload, saved, baseline

    def prepare(self, session_id, body):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            session = self._owner(c, session_id, body.child_id)
            handoff_id = _identity(body.original_action_key)
            existing = c.execute('SELECT * FROM audit_event WHERE audit_id=? AND child_id=? AND action=?',
                                 (handoff_id, body.child_id, PREPARED)).fetchone()
            if existing:
                result = self._read_pointer(c, session_id, body.child_id, existing)
                c.commit()
                return result
            if c.execute('SELECT 1 FROM audit_event WHERE audit_id=?', (self._tombstone_key(handoff_id),)).fetchone():
                return self._response(session_id, body.child_id, 'invalidated',
                    {'handoff_id': handoff_id, 'agent_action_id': 'voice-handoff-' + _hash(
                        [session_id, body.child_id, body.original_action_key])}, reason='VOICE_INTERACTION_HANDOFF_INVALIDATED')
            self.voice._assert_recovery_session(c, session_id, body.child_id)
            row, payload, saved, baseline = self._source(c, session_id, body.child_id, body.original_action_key)
            profile = self.voice._profile_snapshot(body.child_id)
            consent = VoiceControlIntent.text_consent_version(c)
            if saved != {'profile': profile, 'consent_version': list(consent) if consent else None}:
                raise VoiceInteractionConflictError('VOICE_INTERACTION_RECOVERY_CONTEXT_CHANGED')
            deferred = DeferredVoiceOnboardingProgress(childId=body.child_id,
                updatedAt=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')).model_dump()
            pointer = {'version': 1, 'session_id': session_id, 'handoff_id': handoff_id,
                'original_action_key': body.original_action_key, 'source_fingerprint': row['fingerprint'],
                'agent_action_id': 'voice-handoff-' + _hash([session_id, body.child_id, body.original_action_key]),
                'initial_profile_hash': _hash(profile),
                'target_profile_hash': _hash({**profile, 'progress': deferred}),
                'deferred_progress': deferred, 'consent_version': saved['consent_version'], 'session_state': session['state'],
                **baseline}
            c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) '
                "VALUES (?,'child',?,?,?,?)", (handoff_id, PREPARED, body.child_id,
                    json.dumps(pointer, ensure_ascii=False, sort_keys=True), utc_now()))
            result = self._read_pointer(c, session_id, body.child_id,
                c.execute('SELECT * FROM audit_event WHERE audit_id=?', (handoff_id,)).fetchone())
            c.commit()
            return result

    def _read_pointer(self, c, session_id, child_id, audit):
        pointer = json.loads(audit['summary_json'])
        if c.execute('SELECT 1 FROM audit_event WHERE audit_id=?', (self._tombstone_key(audit['audit_id']),)).fetchone():
            return self._response(session_id, child_id, 'invalidated', pointer,
                                  reason='VOICE_INTERACTION_HANDOFF_INVALIDATED')
        try:
            self.voice._assert_recovery_session(c, session_id, child_id)
            session = self._owner(c, session_id, child_id)
            row, payload, saved, baseline = self._source(c, session_id, child_id, pointer['original_action_key'])
            consent = VoiceControlIntent.text_consent_version(c)
            profile_hash = _hash(self.voice._profile_snapshot(child_id))
            if (pointer['session_id'] != session_id or pointer['handoff_id'] != audit['audit_id']
                    or pointer['handoff_id'] != _identity(row['action_key'])
                    or pointer['agent_action_id'] != 'voice-handoff-' + _hash([session_id, child_id, row['action_key']])
                    or pointer['source_fingerprint'] != row['fingerprint']
                    or any(pointer.get(key) != value for key, value in baseline.items())
                    or pointer['consent_version'] != (list(consent) if consent else None)
                    or saved['consent_version'] != pointer['consent_version']
                    or pointer['initial_profile_hash'] != _hash(saved['profile'])
                    or pointer['target_profile_hash'] != _hash({**saved['profile'], 'progress': pointer['deferred_progress']})
                    or profile_hash not in {pointer['initial_profile_hash'], pointer['target_profile_hash']}):
                raise VoiceInteractionConflictError('VOICE_INTERACTION_RECOVERY_CONTEXT_CHANGED')
            for table, baseline in [('learning_input_arrival', 'arrival_rowid'), ('agent_turn', 'turn_rowid')]:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() and c.execute(
                        f'SELECT 1 FROM {table} WHERE session_id=? AND child_id=? AND rowid>? AND client_action_id!=? LIMIT 1',
                        (session_id, child_id, pointer[baseline], pointer['agent_action_id'])).fetchone():
                    raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
            own_claims = _identities(pointer['agent_action_id'], payload.voice_input_id)
            for admission in c.execute('SELECT audit_id FROM audit_event WHERE action=? AND child_id=? AND rowid>? '
                    "AND json_valid(summary_json) AND json_extract(summary_json,'$.session_id')=?",
                    (ACTION, child_id, pointer['claim_rowid'], session_id)):
                if admission['audit_id'] not in own_claims:
                    raise VoiceInteractionConflictError('VOICE_INTERACTION_HANDOFF_SOURCE_CHANGED')
            receipt = HandoffAgentReceipt.model_validate(read_agent_request_receipt(
                self.database, session_id, pointer['agent_action_id'], connection=c))
            if receipt.status != 'unconfirmed':
                status = {'recorded_completed': 'completed', 'recorded_failed': 'failed'}.get(receipt.status, receipt.status)
                return self._response(session_id, child_id, status, pointer, receipt=receipt)
            claimed = any(c.execute('SELECT 1 FROM audit_event WHERE audit_id=?', (identity,)).fetchone()
                          for identity in own_claims)
            if claimed or not read_unsent_authority(c, session_id, child_id, pointer['agent_action_id'],
                                                   payload.transcript, payload.voice_input_id):
                return self._response(session_id, child_id, 'outcome_unknown', pointer, receipt=receipt,
                                      reason='AGENT_HANDOFF_UNCONFIRMED')
            if session['state'] != pointer['session_state']:
                raise VoiceInteractionConflictError('VOICE_INTERACTION_RECOVERY_SCOPE_CHANGED')
            if profile_hash == pointer['initial_profile_hash']:
                return self._response(session_id, child_id, 'needs_deferral', pointer)
            return self._response(session_id, child_id, 'ready', pointer, request=payload)
        except (VoiceInteractionConflictError, ValueError, TypeError, KeyError) as error:
            self._invalidate(c, session_id, child_id, audit['audit_id'])
            return self._response(session_id, child_id, 'invalidated', pointer,
                reason=error.code if isinstance(error, VoiceInteractionConflictError) else 'VOICE_INTERACTION_HANDOFF_INVALIDATED')

    def read(self, session_id, body):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._owner(c, session_id, body.child_id)
            audit = self._latest_pointer(c, session_id, body.child_id)
            if audit is None:
                self.voice._assert_recovery_session(c, session_id, body.child_id)
                return self._response(session_id, body.child_id, 'none')
            result = self._read_pointer(c, session_id, body.child_id, audit)
            c.commit()
            return result

    def invalidate(self, session_id, body):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._owner(c, session_id, body.child_id)
            audit = self._latest_pointer(c, session_id, body.child_id)
            source = self._latest_source(c, session_id, body.child_id)
            if audit:
                self._invalidate(c, session_id, body.child_id, audit['audit_id'])
            if source:
                # A privacy stop can beat prepare while the original model
                # response is in flight. Fence that source before it returns.
                self._invalidate(c, session_id, body.child_id, _identity(source['action_key']))
            c.commit()
            return self._response(session_id, body.child_id, 'invalidated' if audit or source else 'none',
                json.loads(audit['summary_json']) if audit else {'handoff_id': _identity(source['action_key']),
                    'agent_action_id': 'voice-handoff-' + _hash([session_id, body.child_id, source['action_key']])} if source else None,
                reason='VOICE_INTERACTION_HANDOFF_INVALIDATED' if audit or source else None)

    def authorize(self, c, session_id, handoff_id, action_id, transcript, voice_input_id):
        """Called only inside Agent claim's BEGIN IMMEDIATE, before admission."""
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        audit = c.execute('SELECT * FROM audit_event WHERE audit_id=? AND action=?', (handoff_id, PREPARED)).fetchone()
        if not owner or not audit or audit['child_id'] != owner[0]:
            raise AgentRequestOwnershipError('AGENT_HANDOFF_SOURCE_CONFLICT')
        latest = self._latest_pointer(c, session_id, owner[0])
        result = self._read_pointer(c, session_id, owner[0], audit)
        if (not latest or latest['audit_id'] != handoff_id or result.status != 'ready' or not result.request
                or action_id != result.agent_action_id or transcript.strip() != result.request.transcript.strip()
                or voice_input_id != result.request.voice_input_id):
            raise AgentRequestOwnershipError('AGENT_HANDOFF_UNCONFIRMED')


def quarantine_after_restore(live, staged):
    """Restore may preserve history, never a prior tutorial send authority."""
    if live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'").fetchone():
        for row in live.execute('SELECT * FROM audit_event WHERE action=?', (INVALIDATED,)):
            prior = staged.execute('SELECT * FROM audit_event WHERE audit_id=?', (row[0],)).fetchone()
            if prior and tuple(prior) != tuple(row):
                raise AgentRequestOwnershipError('AGENT_HANDOFF_RESTORE_CONFLICT')
            staged.execute('INSERT OR IGNORE INTO audit_event VALUES (?,?,?,?,?,?)', tuple(row))
    # Include live pointers absent from an older snapshot: restoring a backup
    # from before prepare must not make that same source prepare-able again.
    for database in (live, staged):
        if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'").fetchone():
            continue
        for row in database.execute('SELECT audit_id,child_id,summary_json FROM audit_event WHERE action=?', (PREPARED,)):
            pointer = json.loads(row[2])
            staged.execute('INSERT OR IGNORE INTO audit_event VALUES (?,?,?,?,?,?)', (
                VoiceInteractionHandoffService._tombstone_key(row[0]), 'system', INVALIDATED, row[1],
                json.dumps({'version': 1, 'session_id': pointer['session_id'], 'handoff_id': row[0]}), utc_now()))

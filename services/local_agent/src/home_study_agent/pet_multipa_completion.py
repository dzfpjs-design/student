"""Private one-use host completion, not a score, persistence or installation API."""
import hashlib
import json
import threading

from .pet_multipa_epoch import GROUPS, need
from .pet_multipa_protocol import MAX_RESULT

_OWNER = object()
_LOCK = threading.Lock()
_PENDING = None  # At most one invocation, including an unconsumed completion/HOLD.


def _encoded(value, bound=MAX_RESULT):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode()
        need(0 < len(data) <= bound, 'COMPLETION_VALUE_REJECTED')
        return data
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('PET_MULTIPA_COMPLETION_VALUE_REJECTED') from None


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _claim_snapshot(claim):
    from .pet_speaking_candidate_jobs import _Claim
    need(type(claim) is _Claim and set(vars(claim)) == {'job_id', 'token', 'epoch'}, 'COMPLETION_CLAIM_REQUIRED')
    values = tuple(vars(claim)[k] for k in ('job_id', 'token', 'epoch'))
    need(all(type(v) is str and 0 < len(v) <= 200 for v in values), 'COMPLETION_CLAIM_REQUIRED')
    return values


def _binding_snapshot(binding):
    need(type(binding) is dict, 'COMPLETION_BINDING_REQUIRED')
    family, schema = binding.get('task_family'), binding.get('schema')
    need(type(family) is str and type(schema) is str and (family, schema) in {
        ('spoken_response', 'pet_multipa_input_v1'),
        ('read_aloud', 'pet_multipa_reference_input_v1')}, 'COMPLETION_BINDING_REQUIRED')
    need(all(type(binding.get(k)) is str and 0 < len(binding[k]) <= 200
             for k in ('child_id', 'session_id', 'media_id', 'assessment_id')), 'COMPLETION_BINDING_REQUIRED')
    audio = binding.get('audio')
    need(type(audio) is dict and _hash(audio.get('sha256')) and type(audio.get('frames')) is int
         and audio['frames'] > 0 and audio.get('sample_rate') == 16000, 'COMPLETION_BINDING_REQUIRED')

    reference = binding.get('reference')
    if family == 'read_aloud':
        need(type(reference) is dict and _hash(reference.get('text_sha256'))
             and type(reference.get('text_utf8_bytes')) is int
             and 0 < reference['text_utf8_bytes'] <= 24000, 'COMPLETION_BINDING_REQUIRED')
        ref = (reference['text_sha256'], reference['text_utf8_bytes'])
    else:
        need(reference is None, 'COMPLETION_BINDING_REQUIRED')
        ref = (None, 0)
    return _digest(binding), audio['sha256'], audio['frames'], family, ref


class _Opaque:
    __slots__ = ()

    def __repr__(self):
        return '<private worker completion>'

    def __reduce_ex__(self, protocol):
        raise TypeError('PRIVATE_COMPLETION_NOT_SERIALIZABLE')


class _Invocation(_Opaque):
    __slots__ = ()


class _Capability(_Opaque):
    __slots__ = ()


class _AttestedWorkerResult(_Opaque):
    __slots__ = ('_numeric', '_completion')

    @property
    def numeric(self):
        return self._numeric

    @property
    def completion(self):
        return self._completion

    def __setattr__(self, name, value):
        raise AttributeError('PRIVATE_COMPLETION_IMMUTABLE')


def _begin_worker_completion(job_claim, binding):
    global _PENDING
    claim, source = _claim_snapshot(job_claim), _binding_snapshot(binding)
    invocation = _Invocation()
    with _LOCK:
        need(_PENDING is None, 'COMPLETION_BUSY_OR_HELD')
        _PENDING = dict(invocation=invocation, claim=job_claim, claim_snapshot=claim,
                        source=source, phase='PREPARED')
    return invocation


def _state(invocation, owner, phase):
    need(owner is _OWNER and _PENDING is not None and type(invocation) is _Invocation
         and _PENDING['invocation'] is invocation and _PENDING['phase'] == phase,
         'COMPLETION_OWNERSHIP_REQUIRED')
    return _PENDING


def _start(invocation, owner, content, reference, segmented, approved):
    audio = hashlib.sha256(content).hexdigest()
    reference_pin = ((reference['text_sha256'], reference['text_utf8_bytes'])
                     if reference is not None else (None, 0))
    assets = approved.get('asset_hashes')
    need(type(assets) is dict and set(assets) == GROUPS and all(_hash(v) for v in assets.values()),
         'COMPLETION_ASSETS_REQUIRED')
    deployment, assets_digest = _digest(approved), _digest(assets)
    with _LOCK:
        state = _state(invocation, owner, 'PREPARED')
        claim, old_claim = state['claim'], state['claim_snapshot']
    current_claim = _claim_snapshot(claim)
    with _LOCK:
        state = _state(invocation, owner, 'PREPARED')
        need(current_claim == old_claim, 'COMPLETION_CLAIM_CHANGED')
        _, expected_audio, frames, family, expected_reference = state['source']
        need(audio == expected_audio and reference_pin == expected_reference
             and (reference is not None) == (family == 'read_aloud')
             and segmented == (family == 'read_aloud' and frames >= 240000), 'COMPLETION_INPUT_CHANGED')
        state.update(phase='RUNNING', deployment=deployment, assets=assets_digest,
                     audio_bytes=len(content), approved=approved)


def _transport_drained(invocation, owner, process, job, threads):
    # Called only after the original natural/drained checks and pipe close.
    need(process.returncode == 0 and job is not None and len(threads) == 2
         and process.stdin.closed and process.stdout.closed, 'COMPLETION_DRAIN_REQUIRED')
    with _LOCK:
        _state(invocation, owner, 'RUNNING')['transport'] = True


def _decoded(invocation, owner, numeric, nonce, held, held_approved):
    raw_hash, held_deployment = _digest(numeric), _digest(held_approved)
    need(type(numeric) is dict and type(nonce) is str and len(nonce) == 32
         and all(c in '0123456789abcdef' for c in nonce), 'COMPLETION_RESULT_REQUIRED')
    need(type(held) is dict and set(held) == {'installation_epoch_sha256', 'authority_set_sha256',
         'deployment_manifest_sha256'} and all(_hash(v) for v in held.values()), 'COMPLETION_EPOCH_REQUIRED')
    with _LOCK:
        state = _state(invocation, owner, 'RUNNING')
        need(state.get('transport') is True, 'COMPLETION_DRAIN_REQUIRED')
        need(held_deployment == state['deployment'], 'COMPLETION_EPOCH_REQUIRED')
        _, audio, frames, family, reference = state['source']
        if family == 'read_aloud':
            need(numeric.get('audio_sha256') == audio and numeric.get('frames') == frames
                 and numeric.get('reference_sha256') == reference[0]
                 and numeric.get('reference_utf8_bytes') == reference[1], 'COMPLETION_RESULT_CHANGED')
        state.update(phase='DECODED', numeric=numeric, raw_hash=raw_hash,
                     nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(), held=dict(held))


def _hold(invocation, owner):
    with _LOCK:
        need(owner is _OWNER and _PENDING is not None and _PENDING['invocation'] is invocation,
             'COMPLETION_OWNERSHIP_REQUIRED')
        _PENDING['phase'] = 'HOLD'


def _released(invocation, owner):
    with _LOCK:
        _state(invocation, owner, 'DECODED')['phase'] = 'RELEASED'


def _seal(invocation, owner):
    with _LOCK:
        state = _state(invocation, owner, 'RELEASED')
        numeric, claim, snapshot, approved = (state[k] for k in ('numeric', 'claim', 'claim_snapshot', 'approved'))
    need(not state.get('abandoned') and _claim_snapshot(claim) == snapshot and _digest(numeric) == state['raw_hash']
         and _digest(approved) == state['deployment'], 'COMPLETION_SOURCE_CHANGED')
    result = _AttestedWorkerResult()
    capability = _Capability()
    object.__setattr__(result, '_numeric', numeric)
    object.__setattr__(result, '_completion', capability)
    with _LOCK:
        current = _state(invocation, owner, 'RELEASED')
        need(not current.get('abandoned'), 'COMPLETION_SOURCE_CHANGED')
        current.update(phase='SEALED', attested=result, capability=capability)
    return result


def _discard_worker_completion(invocation):
    global _PENDING
    with _LOCK:
        if _PENDING is None or _PENDING['invocation'] is not invocation:
            return False
        if _PENDING['phase'] == 'HOLD':
            return False  # Only normal host retirement can release unknown native ownership.
        if _PENDING['phase'] in {'RUNNING', 'DECODED', 'RELEASED'}:
            _PENDING['abandoned'] = True
            return False  # Host must finish its original cleanup before freeing the slot.
        _PENDING = None
        return True


def _abort(invocation, owner):
    global _PENDING
    with _LOCK:
        need(owner is _OWNER, 'COMPLETION_OWNERSHIP_REQUIRED')
        if _PENDING is None or _PENDING['invocation'] is not invocation:
            return
        if _PENDING['phase'] != 'HOLD':
            _PENDING = None


def _consume_worker_completion(attested, *, invocation, job_claim, binding):
    global _PENDING
    with _LOCK:
        state = _state(invocation, _OWNER, 'SEALED')
        need(type(attested) is _AttestedWorkerResult and state['attested'] is attested
             and attested.completion is state['capability'], 'COMPLETION_CAPABILITY_REQUIRED')
        _PENDING = None  # Irreversible, including a later mismatch or future transaction rollback.
    need(job_claim is state['claim'] and _claim_snapshot(job_claim) == state['claim_snapshot']
         and _binding_snapshot(binding) == state['source'], 'COMPLETION_CLAIM_OR_BINDING_CHANGED')
    need(attested.numeric is state['numeric'] and _digest(attested.numeric) == state['raw_hash'],
         'COMPLETION_RESULT_CHANGED')
    digest, audio, frames, family, reference = state['source']
    receipt = dict(schema='owned_worker_completion_v1', invocation_nonce_sha256=state['nonce_hash'],
        job_id_sha256=hashlib.sha256(state['claim_snapshot'][0].encode()).hexdigest(),
        source_binding_sha256=digest, input_audio_sha256=audio, input_audio_bytes=state['audio_bytes'],
        frames=frames, task_family=family, reference_sha256=reference[0], reference_utf8_bytes=reference[1],
        raw_result_sha256=state['raw_hash'], deployment_sha256=state['deployment'],
        asset_set_sha256=state['assets'], **state['held'], native_exit=0, job_active=0,
        pipe_thread_drain_confirmed=True, owned_releases_confirmed=True,
        score_qualified=False, mastery_claim_allowed=False, certification_claim_allowed=False)
    _encoded(receipt, 4096)
    return receipt

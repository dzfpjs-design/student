"""Local, uncalibrated MultiPA input/result boundary; never an installed assessor.

This module does not load weights, call ASR, write a score, or grant learning.
The original media/review authority is rechecked for every input and result.
"""
import hashlib
import io
import json
import math
import wave
from contextlib import contextmanager

from .pet_speaking_review import prepare_speaking_audio_review, read_speaking_review_audio

MODEL = 'yuwchen/MultiPA:research-candidate'
FIELDS = ('child_id', 'session_id', 'media_id', 'assessment_id', 'prompt_id',
          'item_version', 'voice_input_id', 'source_version', 'attempt_id',
          'evidence_id', 'assessment_fingerprint', 'binding_version', 'review_revision')


def require(value, code):
    if not value:
        raise ValueError('PET_MULTIPA_' + code)


@contextmanager
def snapshot(c):
    own = not c.in_transaction
    if own:
        c.execute('BEGIN')
    try:
        yield
    finally:
        if own:
            c.rollback()


def prepare_input(c, session_id, *, media_id, assessment_id, media_root, data_encryption_key=None):
    """Return exact original WAV bytes and versioned binding, only to local code.

    This first slice admits the upstream single-segment spoken-response path.
    Read-aloud needs a separately bound reference transcript; the upstream ASR
    text is not silently substituted for that target. Long-file upstream
    aggregation is not admitted because it has independent implementation bugs.
    """
    args = dict(media_id=media_id, assessment_id=assessment_id, media_root=media_root,
                data_encryption_key=data_encryption_key)
    with snapshot(c):
        packet = prepare_speaking_audio_review(c, session_id, **args)
        reference = None
        if packet['task_family'] == 'read_aloud':
            from .pet_oral_source import resolve_read_aloud_reference
            reference, _ = resolve_read_aloud_reference(c, packet['child_id'], packet['diagnostic_item_id'], packet['item_version'])
        else:
            require(packet['task_family'] == 'spoken_response', 'REFERENCE_TRANSCRIPT_ADAPTER_REQUIRED')
        content = read_speaking_review_audio(c, session_id, audio_sha256=packet['audio']['sha256'], **args)
        with wave.open(io.BytesIO(content), 'rb') as w:
            if packet['task_family']=='read_aloud' and w.getnframes()>=240000:
                from .pet_multipa_reference_segments import MAX_FRAMES,MAX_BYTES
                require(w.getcomptype()=='NONE' and w.getnchannels()==1 and w.getsampwidth()==2
                        and w.getframerate()==16000 and 240000<=w.getnframes()<=MAX_FRAMES and len(content)<=MAX_BYTES,'REFERENCE_LONG_PCM_REQUIRED')
            else:
                require(w.getcomptype() == 'NONE' and w.getnchannels() == 1 and w.getsampwidth() == 2
                        and w.getframerate() == 16000 and 0 < w.getnframes() < 240000, 'PCM16_MONO_SHORT_SEGMENT_REQUIRED')
            require(w.getnframes() == packet['audio']['frames'], 'FRAME_IDENTITY_CHANGED')
        require(hashlib.sha256(content).hexdigest() == packet['audio']['sha256'], 'AUDIO_IDENTITY_CHANGED')
        binding = {k: packet[k] for k in FIELDS}
        binding.update(schema='pet_multipa_input_v1', audio=dict(packet['audio']), task_family=packet['task_family'])
        if reference is not None:
            binding.update(schema='pet_multipa_reference_input_v1', reference=reference)
        return binding, content


def candidate_result(c, session_id, *, binding, result, asset_hashes, **args):
    """Recheck current original audio; validate raw model outputs, not rubric scores.

    These untrusted raw values may be displayed as an uncalibrated experiment.
    They cannot be passed as human audio evidence or calibration receipts.
    No automatic result persistence exists in the current production contract;
    the existing authorized RubricEvidenceService.review remains the sole human
    review transaction. A future auto scorer needs its own reviewed calibration
    and immutable event contract before it may write/qualify learning evidence.
    """
    with snapshot(c):
        current, _ = prepare_input(c, session_id, **args)
        require(binding == current, 'ORIGINAL_BINDING_CHANGED')
        if current['task_family']=='read_aloud' and current['audio']['frames']>=240000:
            from .pet_multipa_reference_segments import candidate
            return candidate(current,result,asset_hashes,MODEL)
        if current['task_family']=='read_aloud':
            from .pet_multipa_reference import candidate
            return candidate(current,result,asset_hashes,MODEL)
        require(current['task_family'] == 'spoken_response', 'REFERENCE_EXECUTOR_UNAVAILABLE')
        keys = {'uttr_acc', 'uttr_fluency', 'uttr_prosodic', 'uttr_total', 'word_acc',
                'word_stress', 'word_total', 'word_text', 'transcript_S', 'transcript_W'}
        require(type(result) is dict and set(result) == keys, 'EXACT_UPSTREAM_RESULT_REQUIRED')
        def number(n):
            require(type(n) in (int, float) and math.isfinite(n) and abs(n) <= 1000000, 'FINITE_RAW_VALUE_REQUIRED')
            return float(n)
        scalar = {k: number(result[k]) for k in ('uttr_acc', 'uttr_fluency', 'uttr_prosodic', 'uttr_total')}
        words = result['word_text']
        require(type(words) is list and 0 < len(words) <= 512 and all(type(w) is str and 0 < len(w) <= 80 for w in words), 'WORD_SHAPE_REQUIRED')
        vectors = {}
        for k in ('word_acc', 'word_stress', 'word_total'):
            require(type(result[k]) is list and len(result[k]) == len(words), 'WORD_SHAPE_REQUIRED')
            vectors[k] = [number(n) for n in result[k]]
        require(all(type(result[k]) is str and 0 < len(result[k]) <= 16000 for k in ('transcript_S', 'transcript_W')), 'TRANSCRIPT_SHAPE_REQUIRED')
        required_assets = {'multipa', 'charsiu', 'hubert', 'roberta', 'whisper_medium_en', 'whisper_base_en'}
        require(type(asset_hashes) is dict and set(asset_hashes) == required_assets
                and all(type(v) is str and len(v) == 64 and all(ch in '0123456789abcdef' for ch in v) for v in asset_hashes.values()), 'ASSET_IDENTITIES_REQUIRED')
        value = {'schema': 'pet_multipa_raw_candidate_v1', 'binding': current, 'model': MODEL,
                 'asset_hashes': dict(asset_hashes), 'asset_authorization': 'not_verified_by_this_converter',
                 'raw_sentence_outputs': scalar, 'raw_word_outputs': vectors, 'word_text': list(words),
                 'unverified_transcripts': {k: result[k] for k in ('transcript_S', 'transcript_W')},
                 'status': 'uncalibrated_model_candidate_not_rubric_score', 'persistence_status': 'not_persisted',
                 'assessor_installed': False, 'missing_dimensions': ['audio_text_alignment', 'intelligibility'],
                 'skill_qualified': False, 'mastery_claim_allowed': False, 'certification_claim_allowed': False,
                 'local_only': True, 'cloud_transmission_allowed': False}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        require(len(encoded) <= 262144, 'RESULT_BOUND')
        value['candidate_sha256'] = hashlib.sha256(encoded).hexdigest()
        return value

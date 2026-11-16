"""Versioned private reference input and numeric-only uncalibrated output.

No deployment, permission, score, human review or mastery is granted here.
Original reference text exists only in the one owned worker input frame.
"""
import hashlib, math
from .pet_multipa_epoch import need, GROUPS
REFERENCE_ADAPTER = 'multipa_reference_short_v1'
REFERENCE_INPUT = 'pet_multipa_reference_text_v1'
REFERENCE_RESULT = 'pet_multipa_reference_raw_v1'
SENTENCE = {'uttr_acc','uttr_fluency','uttr_prosodic','uttr_total'}
WORDS = {'word_acc','word_stress','word_total'}

def capable(approved):
    if type(approved) is not dict:return False
    authority=approved.get('authority_sha256');declaration=approved.get('reference_adapter')
    if type(authority) is not dict or type(declaration) is not dict or set(declaration)!={'version','deployment_sha256'}:return False
    digest=authority.get('deployment.json')
    return (type(digest) is str and len(digest)==64 and all(c in '0123456789abcdef' for c in digest)
            and declaration['version']==REFERENCE_ADAPTER and declaration['deployment_sha256']==digest)

def text_input(text):
    need(type(text) is str and 0 < len(text) <= 6000 and bool(text.strip()) and '\x00' not in text, 'REFERENCE_TEXT_REJECTED')
    data = text.encode('utf-8'); need(len(data) <= 24000, 'REFERENCE_TEXT_REJECTED')
    return dict(schema=REFERENCE_INPUT, text=text, text_sha256=hashlib.sha256(data).hexdigest(), text_utf8_bytes=len(data))

def validate_text(value):
    need(type(value) is dict and set(value) == {'schema','text','text_sha256','text_utf8_bytes'}, 'REFERENCE_INPUT_REJECTED')
    current = text_input(value['text'])
    need(type(value['text_utf8_bytes']) is int and value == current, 'REFERENCE_INPUT_REJECTED')
    return current

def prepare_reference(c, binding):
    from .pet_oral_source import resolve_read_aloud_reference
    need(type(binding) is dict and binding.get('task_family') == 'read_aloud', 'REFERENCE_BINDING_REQUIRED')
    identity, text = resolve_read_aloud_reference(c, binding['child_id'], binding['reference']['diagnostic_item_id'], binding['item_version'])
    need(identity == binding['reference'], 'REFERENCE_CHANGED')
    payload = text_input(text)
    need(payload['text_sha256'] == identity['text_sha256'] and payload['text_utf8_bytes'] == identity['text_utf8_bytes'], 'REFERENCE_CHANGED')
    return payload

def validate_result(value):
    need(type(value) is dict and set(value) == {'schema','adapter','audio_sha256','reference_sha256','reference_utf8_bytes','frames','sentence','words','word_frames'}, 'REFERENCE_RESULT_REJECTED')
    need(value['schema'] == REFERENCE_RESULT and value['adapter'] == REFERENCE_ADAPTER, 'REFERENCE_RESULT_REJECTED')
    for key in ('audio_sha256','reference_sha256'):
        need(type(value[key]) is str and len(value[key]) == 64 and all(c in '0123456789abcdef' for c in value[key]), 'REFERENCE_RESULT_REJECTED')
    need(type(value['reference_utf8_bytes']) is int and 0 < value['reference_utf8_bytes'] <= 24000 and type(value['frames']) is int and 0 < value['frames'] < 240000, 'REFERENCE_RESULT_REJECTED')
    def number(v): return type(v) in (int,float) and math.isfinite(v) and abs(v) <= 1000000
    need(type(value['sentence']) is dict and set(value['sentence']) == SENTENCE and all(number(v) for v in value['sentence'].values()), 'REFERENCE_RESULT_REJECTED')
    spans = value['word_frames']; need(type(spans) is list and 0 < len(spans) <= 512, 'REFERENCE_RESULT_REJECTED')
    last = 0
    for span in spans:
        need(type(span) is list and len(span) == 2 and all(type(v) is int for v in span) and last <= span[0] < span[1] <= value['frames'], 'REFERENCE_RESULT_REJECTED')
        last = span[1]
    need(type(value['words']) is dict and set(value['words']) == WORDS and all(type(v) is list and len(v) == len(spans) and all(number(n) for n in v) for v in value['words'].values()), 'REFERENCE_RESULT_REJECTED')
    return value

def candidate(binding, result, asset_hashes, model):
    value = validate_result(result)
    need(value['audio_sha256'] == binding['audio']['sha256'] and value['frames'] == binding['audio']['frames'] and value['reference_sha256'] == binding['reference']['text_sha256'] and value['reference_utf8_bytes'] == binding['reference']['text_utf8_bytes'], 'REFERENCE_RESULT_BINDING_CHANGED')
    need(type(asset_hashes) is dict and set(asset_hashes) == GROUPS and all(type(v) is str and len(v) == 64 and all(c in '0123456789abcdef' for c in v) for v in asset_hashes.values()), 'REFERENCE_ASSETS_REQUIRED')
    return dict(schema=REFERENCE_RESULT, binding=binding, model=model, asset_hashes=dict(asset_hashes), raw_sentence_outputs=value['sentence'], raw_word_outputs=value['words'], word_count=len(value['word_frames']), alignment_frames=value['word_frames'], adapter=REFERENCE_ADAPTER, status='uncalibrated_model_candidate_not_rubric_score', assessor_installed=False, skill_qualified=False, mastery_claim_allowed=False, certification_claim_allowed=False, local_only=True, cloud_transmission_allowed=False)

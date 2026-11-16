"""Local quotation redaction for question-role classification, never commands."""
import hashlib
import json

VERSION = 'tutor_question_role_projection_v1'
ROLE_VERSION = 'tutor_question_projected_role_v1'
PLACEHOLDER = '[本地引用]'
PAIRS = {'“': '”', '「': '」', '『': '』', '"': '"'}


def project(transcript):
    if not isinstance(transcript, str) or not 1 <= len(transcript) <= 2000:
        return None
    chunks, spans, cursor = [], [], 0
    while cursor < len(transcript):
        start = cursor
        char = transcript[cursor]
        if char in PAIRS:
            end = transcript.find(PAIRS[char], start + 1)
            if end <= start + 1 or any(c in set(PAIRS) | set(PAIRS.values()) for c in transcript[start + 1:end]):
                return None
            quoted = transcript[start:end + 1]
            spans.append({'start': start, 'end': end + 1, 'sha256': hashlib.sha256(quoted.encode()).hexdigest()})
            chunks.append(PLACEHOLDER)
            cursor = end + 1
        elif char in PAIRS.values():
            return None
        else:
            chunks.append(char)
            cursor += 1
    if not spans:
        return None
    text = ''.join(chunks)
    if len(text.replace(PLACEHOLDER, '').strip(' ，,。.!！?？')) < 2:
        return None
    return text, {'version': VERSION, 'original_sha256': hashlib.sha256(transcript.encode()).hexdigest(),
                  'projected_sha256': hashlib.sha256(text.encode()).hexdigest(), 'spans': spans}


def parse(content, projected_text):
    value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != {'role', 'control', 'quote', 'confidence'}
            or value['quote'] != projected_text or type(value['confidence']) not in (int, float)
            or not 0 <= value['confidence'] <= 1):
        raise ValueError('TUTOR_QUESTION_ROLE_INVALID')
    if value['role'] != 'question' or value['control'] is not None or value['confidence'] < .82:
        return {'status': 'clarify', 'reason': 'TUTOR_QUESTION_ROLE_UNCERTAIN'}
    return {'status': 'non_control', 'role': 'question', 'role_version': ROLE_VERSION}

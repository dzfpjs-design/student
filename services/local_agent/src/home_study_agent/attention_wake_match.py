"""Finite local attention matching. Never an answer, tool or identity grant."""
import re
from .attention_pinyin_data import SYLLABLES
from .voice_controls import PRIVACY_STOP_PHRASES, CAMERA_STOP_PHRASES


def compact(value):
    return re.sub(r'[\s，。！？、,.!?;；:："“”\'‘’（）()【】\[\]-]', '', value).lower()


def valid_attention_word(value):
    return (type(value) is str and 2 <= len(value) <= 12
            and re.fullmatch(r'[A-Za-z0-9\u3400-\u9fff]{2,12}', value) is not None
            and not value.isdigit())


def bare_attention_match(transcript, word):
    if not valid_attention_word(word) or type(transcript) is not str:
        return False
    heard, target = compact(transcript), compact(word)
    if heard == target:
        return True
    if (len(heard) != len(target) or not 2 <= len(target) <= 6
            or not re.fullmatch(r'[\u3400-\u9fff]+', target)
            or not re.fullmatch(r'[\u3400-\u9fff]+', heard)):
        return False
    for left, right in zip(heard, target):
        if left == right:
            continue
        a, b = SYLLABLES.get(left), SYLLABLES.get(right)
        if not a or not b or not set(a).intersection(b):
            return False
    return True


def playback_contains_attention(text, word):
    if not valid_attention_word(word) or type(text) is not str:
        return True  # Unknown reference must not permit a wake.
    text, target = compact(text), compact(word)
    return any(bare_attention_match(text[index:index + len(target)], word)
               for index in range(max(0, len(text) - len(target) + 1)))


def registered_prefix_remainder(text, word):
    """Only repeated registered-name prefixes, never arbitrary instruction text."""
    if not valid_attention_word(word) or type(text) is not str:
        return None
    text, size = compact(text), len(compact(word))
    offset = 0
    while offset + size <= len(text) and bare_attention_match(text[offset:offset + size], word):
        offset += size
    return text[offset:] if offset else None


# These are denials of attention only, never execution or persistence of privacy
# controls. Reuse the existing finite device vocabulary to avoid alias drift.
_ATTENTION_PRIVACY_DENY = tuple(compact(phrase) for phrase in (
    PRIVACY_STOP_PHRASES + CAMERA_STOP_PHRASES + (
        '暂停监听', '关闭监听', '暂停观察', '不要监听', '别监听', '不要录音', '别录音',
        '关掉摄像头')))


def privacy_control_mixture(text):
    text = compact(text)
    return any(phrase in text for phrase in _ATTENTION_PRIVACY_DENY)

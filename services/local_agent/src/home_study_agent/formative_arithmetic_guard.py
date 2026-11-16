"""Reject false numeric equalities before speaking formative model feedback.

This checks bounded integer four-operation equalities, not general mathematics
or correctness of a teaching explanation. Source quotations are not assertions.
"""
import re
from .curriculum_supply import CurriculumSupplyError, _arithmetic_answer


# Keep unsupported math notation in the same expression, so a suffix of
# 2²-4×1×3 or sqrt(25) is never evaluated as its own calculation.
_MATH_RUN = re.compile(r'(?:[0-9+*/=() \t\-^²³¹⁰⁴⁵⁶⁷⁸⁹⁺⁻√∛±%<>!≤≥≠·{}\[\]\\$]|\.(?=[0-9]))+')
_FOUR_OPERATIONS = re.compile(r'[0-9+*/() \t\-]+')
_SYMBOL = re.compile(r'[A-Za-z_\u0370-\u03ff]')


def _arithmetic_value(part):
    part = part.strip()
    if not _FOUR_OPERATIONS.fullmatch(part):
        return None
    if len(part) > 200:
        raise ValueError('FORMATIVE_ARITHMETIC_LIMIT')
    try:
        return _arithmetic_answer('计算：'+part+'。')
    except CurriculumSupplyError as error:
        # A numeric-looking fragment may end at a prose annotation or an
        # unknown operand. Only compare independently parseable operands.
        if str(error) == 'CURRICULUM_QUESTION_FORM_UNSUPPORTED' and not isinstance(error.__cause__, ZeroDivisionError):
            return None
        raise


def assert_numeric_equalities(text):
    normalized = text.translate(str.maketrans({'×': '*', '·': '*', '÷': '/', '−': '-', '＝': '=', '（': '(', '）': ')'}))
    normalized = re.sub(r'\\(?:times|cdot)(?![a-zA-Z])', '*', normalized)
    normalized = re.sub(r'\\div(?![a-zA-Z])', '/', normalized)
    normalized = re.sub(r'(?<=\d)[ \t]*[xX][ \t]*(?=\d)', '*', normalized)
    for match in _MATH_RUN.finditer(normalized):
        parts = match[0].split('=')
        if len(parts) < 2:
            continue
        start, end = match.span()
        before, after = normalized[start-1:start], normalized[end:end+1]
        standalone_before = not _SYMBOL.fullmatch(normalized[start-2:start-1])
        standalone_after = not _SYMBOL.fullmatch(normalized[end+1:end+2])
        if _SYMBOL.fullmatch(before) and (parts[0] == parts[0].lstrip()
                                        or standalone_before):
            parts[0] = ''
        if _SYMBOL.fullmatch(after) and (parts[-1] == parts[-1].rstrip() or standalone_after):
            parts[-1] = ''
        operands = [_arithmetic_value(part) for part in parts]
        if any(left is not None and right is not None and left != right for left, right in zip(operands, operands[1:])):
            raise ValueError('FORMATIVE_FALSE_NUMERIC_EQUALITY')

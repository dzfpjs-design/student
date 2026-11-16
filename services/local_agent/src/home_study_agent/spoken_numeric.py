"""Bounded whole-utterance numeric scoring, never substring answer extraction."""
import re
import unicodedata
from fractions import Fraction

FORMAT = 'reviewed_spoken_numeric_v1'
SCHEMA = 'specialized_closed_numeric_v1'
KINDS = {'integer', 'rational'}
DIGITS = '零一二三四五六七八九'


def _canonical(number):
    if number == 0:
        return '零'
    if number >= 10000:
        high, low = divmod(number, 10000)
        return _canonical(high) + '万' + (('零' if low < 1000 else '') + _canonical(low) if low else '')
    result, pending = '', False
    for unit, label in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
        digit, number = divmod(number, unit)
        if digit:
            result += ('零' if pending else '') + DIGITS[digit] + label
            pending = False
        elif result and number:
            pending = True
    return result[1:] if result.startswith('一十') else result


def _integer(value):
    if re.fullmatch(r'[0-9]{1,8}', value):
        return int(value)
    value = value.replace('〇', '零').replace('两', '二')
    if not value or not re.fullmatch('[零一二三四五六七八九十百千万]+', value):
        raise ValueError('not an integer')
    if all(char in DIGITS for char in value):
        # No digit-dictation contract: "two three" may mean alternatives, not 23.
        if len(value) != 1:
            raise ValueError('ambiguous digit sequence')
        return DIGITS.index(value)
    total, section, digit = 0, 0, 0
    for char in value:
        if char in DIGITS:
            digit = DIGITS.index(char)
        elif char == '万':
            total += (section + digit) * 10000
            section, digit = 0, 0
        else:
            section += (digit or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            digit = 0
    number = total + section + digit
    canonical = _canonical(number) if number < 100000000 else ''
    # Accept the optional leading one in ten, not malformed place-value strings.
    if value != canonical and not (canonical.startswith('十') and value == '一' + canonical):
        raise ValueError('ambiguous integer')
    return number


def reference(value, kind):
    if (kind not in KINDS or not isinstance(value, str) or len(value) > 32
            or not re.fullmatch(r'[+-]?[0-9]{1,8}(?:/[0-9]{1,8}|\.[0-9]{1,8})?', value)):
        raise ValueError('invalid numeric reference')
    result = Fraction(value)
    if kind == 'integer' and (result.denominator != 1 or '/' in value or '.' in value):
        raise ValueError('integer reference required')
    return result


def parse(answer, kind):
    if kind not in KINDS or not isinstance(answer, str) or len(answer) > 100:
        return None
    value = unicodedata.normalize('NFKC', answer).strip()
    value = re.sub(r'^(?:答案是|答案为|我的答案是)', '', value).strip()
    # A bang can be factorial notation, so it cannot be discarded as decoration.
    value = re.sub(r'[。.]$', '', value).strip()
    sign = 1
    if value.startswith(('-', '负', '+', '正')):
        sign = -1 if value[0] in '-负' else 1
        value = value[1:]
    try:
        if kind == 'rational' and ('分之' in value or '/' in value):
            if '分之' in value:
                denominator, numerator = value.split('分之')
            else:
                numerator, denominator = value.split('/')
            result = Fraction(_integer(numerator), _integer(denominator))
        elif kind == 'rational' and ('.' in value or '点' in value):
            integer, decimal = re.split(r'[.点]', value)
            if not 1 <= len(decimal) <= 8:
                raise ValueError('decimal too long')
            decimal = decimal.replace('〇', '零')
            if not (re.fullmatch(r'[0-9]+', decimal) or all(c in DIGITS for c in decimal)):
                raise ValueError('invalid decimal digits')
            digits = ''.join(str(DIGITS.index(c)) if c in DIGITS else c for c in decimal)
            result = Fraction(_integer(integer)) + Fraction(int(digits), 10 ** len(digits))
        else:
            result = Fraction(_integer(value))
        return sign * result
    except (ValueError, ZeroDivisionError):
        return None


def score(rubric, answer):
    try:
        if set(rubric) != {'local_rule', 'answer_format', 'numeric_kind', 'exact_answers'}:
            return None
        kind, answers = rubric['numeric_kind'], rubric['exact_answers']
        if not isinstance(answers, list) or not 1 <= len(answers) <= 24:
            return None
        references = {reference(value, kind) for value in answers}
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return None
    value = parse(answer, kind)
    # Ambiguous/unsupported speech needs clarification, not a false low score.
    return None if value is None else float(value in references)


def task_supported(prompt, kind):
    """Necessary exclusions, not an inference of mathematical/pedagogical validity."""
    if kind not in KINDS or not isinstance(prompt, str):
        return False
    value = unicodedata.normalize('NFKC', prompt)
    # These obligations require more than equality of one dimensionless scalar.
    excluded = (r'单位|量纲|百分|千分|最简|约分|带分数|保留|四舍五入|精确到|近似|有效数字|科学[计记]数'
                r'|进制|前导零|补零|大写|小写|罗马|中文数字|汉字数字|编号|编码|序列|逐位|连写|从小到大|从大到小|升序|降序'
                r'|(?:写|用|输出|答案|表示).{0,16}(?:[零一二三四五六七八九十0-9]+位|位数固定)'
                r'|说明|解释|理由|证明|推导|过程|算式|表达式|方程|不等式'
                r'|分别(?:写|说|求|答)|依次(?:写|说|答)|列出|所有解|各是多少|多个答案|两个答案'
                r'|厘米|毫米|千米|公里|平方米|立方米|千克|公斤|毫升|摄氏|华氏|时速'
                r'|[%‰℃°$￥¥€£]'
                r'|(?:化|写|转|改|表示|用|以|提交|给出|回答|说出|输出|答案).{0,16}(?:小数|分数|比例)'
                r'|\b(?:unit|percent|percentage|round|nearest|simplest|approximate|explain|prove|expression|equation)\b'
                r'|\b(?:decimal places|significant figures|lowest terms|scientific notation|mixed number)\b'
                r'|\b(?:binary|hexadecimal|octal|roman|leading zeros?|zero.pad(?:ded)?|digit sequence|ascending|descending)\b'
                r'|\b(?:write|express|convert|give|answer)\b.{0,40}\b(?:digits?|numerals?|base)\b'
                r'|\b(?:write|express|convert|give|answer)\b.{0,40}\b(?:decimal|fraction|ratio)\b'
                r'|\b(?:cm|mm|km|kg|ml|m/s|km/h)\b')
    return re.search(excluded, value, re.IGNORECASE) is None

"""Explicit reviewed ordered numeric answers; ambiguity has no score."""
import re
import unicodedata

from .spoken_numeric import KINDS, parse as scalar, reference

FORMAT='reviewed_spoken_numeric_tuple_v1'
FIELDS={'local_rule','answer_format','numeric_kind','tuple_size','exact_answers'}


def validate(rubric):
    if (not isinstance(rubric,dict) or set(rubric)!=FIELDS or rubric['local_rule']!='local_exact_v1'
            or rubric['answer_format']!=FORMAT or rubric['numeric_kind'] not in KINDS
            or type(rubric['tuple_size']) is not int or not 2<=rubric['tuple_size']<=8
            or not isinstance(rubric['exact_answers'],list) or not 1<=len(rubric['exact_answers'])<=24):
        raise ValueError('invalid reviewed numeric tuple')
    references=[]
    for answer in rubric['exact_answers']:
        if not isinstance(answer,str) or not 0<len(answer)<=200:
            raise ValueError('invalid tuple reference')
        parts=unicodedata.normalize('NFKC',answer).split(',')
        if len(parts)!=rubric['tuple_size']:
            raise ValueError('invalid tuple reference count')
        references.append(tuple(reference(part.strip(),rubric['numeric_kind']) for part in parts))
    if len(set(references))!=len(references):
        raise ValueError('duplicate tuple reference')
    return references


def score(rubric,answer):
    try:
        references=validate(rubric)
    except (ValueError,TypeError,KeyError,ZeroDivisionError):
        return None
    if not isinstance(answer,str) or not 0<len(answer)<=200:
        return None
    value=unicodedata.normalize('NFKC',answer).strip()
    value=re.sub(r'^(?:我的答案是|答案是|答案为)','',value).strip()
    value=re.sub(r'[。]$','',value).strip()
    parts=re.split(r'[,、;和与]',value)
    if len(parts)!=rubric['tuple_size']:
        return None
    values=tuple(scalar(part.strip(),rubric['numeric_kind']) for part in parts)
    if any(number is None for number in values):
        return None
    return values in references

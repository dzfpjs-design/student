import json
import pytest
from home_study_agent.voice_control_intent import VoiceControlIntent


@pytest.mark.parametrize('control,quote',[
    ('oral.retry','先不要再说一次，我想歇一下'),
    ('oral.independent_check','今天不要换新题了'),
    ('oral.retry','“再说一次”是什么意思'),
    ('oral.retry','我说错了，先别执行'),
])
def test_model_label_cannot_authorize_negated_or_quoted_retry(control,quote):
    value={'role':'control','control':control,'quote':quote,'confidence':.99}
    assert VoiceControlIntent.parse(json.dumps(value),quote)['status']!='control'


@pytest.mark.parametrize('control,quote',[
    ('oral.retry','我想按刚才的建议补充一下，再说一次'),
    ('oral.independent_check','换一道我没有做过的口语题，让我自己试试'),
    ('oral.independent_check','先不重说旧题，换道新题'),
])
def test_positive_semantics_keep_exact_quote(control,quote):
    value={'role':'control','control':control,'quote':quote,'confidence':.99}
    assert VoiceControlIntent.parse(json.dumps(value),quote)=={'status':'control','control':control}
    value['quote']='模型改写的请求'
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(json.dumps(value),quote)

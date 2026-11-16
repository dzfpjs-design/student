import json

import pytest

from home_study_agent.voice_control_intent import VoiceControlIntent


@pytest.mark.parametrize('text,kind', [
    ('我们先进行下一个任务吧','next'),
    ('那，咱们接着学吧','continue'),
    ('继续任务','continue'),
    ('我们先继续任务吧','continue'),
    ('开始根的判别式','named'),
    ('我们先做英语听力练习吧','named'),
    ('不要开始根的判别式',None),
    ('如果开始根的判别式会怎样',None),
    ('老师说开始根的判别式','reported'),
    ('不要继续任务','hold'),
    ('他说继续任务','reported'),
    ('继续任务是什么意思',None),
    ('如果继续任务会怎样',None),
    ('接下来要做什么？','describe'),
    ('别开始','hold'),
    ('他说开始任务','reported'),
    ('上一项没做好','unfinished'),
    ('先别开始下一项，上一项我还没做完',None),
    ('书上说“开始任务”，这是什么意思',None),
    ('如果我们开始下一项会怎样',None),
    ('开始下一项和结束下一项是什么意思',None),
    ('90',None),
    ('My answer begins with a description of the picture.',None),
])
def test_offline_grammar_is_full_utterance_not_substring(text,kind):
    assert VoiceControlIntent.local_task_navigation(text)==kind


@pytest.mark.parametrize('text,control,status', [
    ('接着安排好的下一段来吧','task.start','control'),
    ('别担心，开始下一项','task.start','control'),
    ('别开始','task.start','clarify'),
    ('他说开始任务','task.start','clarify'),
    ('上一项没做好','task.start','clarify'),
    ('接下来做什么','task.start','clarify'),
    ('接下来做什么？','planning.describe','control'),
])
def test_semantic_command_keeps_query_negation_and_reporting_boundaries(text,control,status):
    result=VoiceControlIntent.parse(json.dumps({'role':'control','control':control,'quote':text,'confidence':.99}),text)
    assert result['status']==status


def test_exact_quote_is_required_and_resume_keeps_mandatory_selector():
    with pytest.raises(ValueError,match='CONTROL_INTENT_INVALID'):
        VoiceControlIntent.parse(json.dumps({'role':'control','control':'task.start','quote':'开始下一项','confidence':.99}),
            '他说开始下一项')
    assert VoiceControlIntent.parse(json.dumps({'role':'control','control':'task.resume',
        'quote':'先继续英语','confidence':.99}),'先继续英语')['reason']=='CONTROL_RESUME_SELECTOR_REQUIRED'

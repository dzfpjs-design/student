import asyncio
from types import SimpleNamespace
import pytest

from home_study_agent import local_playback_control as control
from home_study_agent.voice_controls import PRIVACY_STOP_PHRASES, CAMERA_STOP_PHRASES
from test_local_playback_control import Media, payload


def request(**change):
    values=payload().model_dump()
    values['attention_wake_word']='小知'
    values.update(change)
    return SimpleNamespace(**values)


def recognize(text, p=None, media=None):
    return asyncio.run(control.LocalPlaybackControl(media or Media(text)).recognize('public-session',p or request()))


@pytest.mark.parametrize('text',['小知','小 智！','小支','小知，小知','喂，小知，帮我提交答案'])
def test_bare_registered_name_is_only_an_ephemeral_attention_control(text):
    media=Media(text);result=recognize(text,media=media)
    assert result['status']=='recognized' and result['control']=='wake_companion'
    assert result['cloud_used'] is result['persisted'] is result['learning_evidence'] is False
    assert set(result)=={'status','control','playback_lease_id','playback_generation','capture_epoch','cloud_used','persisted','learning_evidence'}
    assert media.calls==[{'prefer_cloud':False,'session':None,'purpose':'agent_command'}]


@pytest.mark.parametrize('current',['小知，接着看这一题。','你刚才叫的是小智吗？'])
def test_name_in_playback_rejects_its_reflection_but_does_not_disable_original_stop(current):
    p=request(current_playback_text=current)
    assert recognize('小知',p)['status']=='echo_rejected'
    assert recognize('暂停',p)['control']=='stop_playback'


@pytest.mark.parametrize('text',['小伴','没有注册的称呼','x'*513+'小知'])
def test_foreign_name_or_overlong_result_cannot_be_attention(text):
    assert recognize(text)['control'] is None


def test_unregistered_name_and_pcm_source_do_not_gain_attention():
    assert recognize('小知',request(attention_wake_word=None))['control'] is None
    media=Media('小知')
    result=recognize('小知',request(source_kind='local_pcm'),media)
    assert result['control'] is None and media.calls==[]


@pytest.mark.parametrize('text',['小知，暂停','小智小知停止播放'])
def test_registered_prefix_original_stop_is_stop_without_attention_or_any_remaining_action(text):
    result=recognize(text)
    assert result['control']=='stop_playback'
    assert result['cloud_used'] is result['persisted'] is result['learning_evidence'] is False
    assert 'transcript' not in result and 'action' not in result
    assert recognize(text,request(current_playback_text='这里可说暂停或停止播放。'))['status']=='echo_rejected'


@pytest.mark.parametrize('phrase', PRIVACY_STOP_PHRASES + CAMERA_STOP_PHRASES)
@pytest.mark.parametrize('template',['小知，{}','喂，小智，请{}吧','小知小知，请{}了'])
def test_privacy_mixture_never_acknowledges_or_claims_a_privacy_change(phrase, template):
    result=recognize(template.format(phrase))
    assert result['status']=='not_control' and result['control'] is None
    assert result['persisted'] is result['learning_evidence'] is False


def test_source_change_and_cloud_decoder_are_rejected():
    class Changed(Media):
        def _recognize_wav(self,*a,**kw):
            self.child='other-child'
            return super()._recognize_wav(*a,**kw)
    for media in (Changed('小知'),Media('小知',cloud=True)):
        assert recognize('小知',media=media)['control'] is None


def test_attention_and_stop_share_the_original_single_decoder():
    async def scenario():
        import threading
        entered=threading.Event();finish=threading.Event()
        class Slow(Media):
            def _recognize_wav(self,*a,**kw):
                entered.set();finish.wait(1)
                return super()._recognize_wav(*a,**kw)
        service=control.LocalPlaybackControl(Slow('小知'))
        pending=asyncio.create_task(service.recognize('public-session',request()))
        assert await asyncio.to_thread(entered.wait,1)
        other=await service.recognize('public-session',request(attention_wake_word=None))
        assert other['status']=='busy'
        finish.set();assert (await pending)['control']=='wake_companion'
    asyncio.run(scenario())


def test_finite_phonetic_scope_unknowns_lengths_and_dictionary_bounds():
    from home_study_agent.attention_wake_match import bare_attention_match,playback_contains_attention
    from home_study_agent.attention_pinyin_data import SYLLABLES
    import re
    assert bare_attention_match('虫明','重名')  # Deliberately ambiguous low-risk attention only.
    assert bare_attention_match('㐀知','㐀知')
    assert not bare_attention_match('㐀知','㐁知')
    assert bare_attention_match('小知知知知知知','小知知知知知知')
    assert not bare_attention_match('小智知知知知知','小知知知知知知')
    assert bare_attention_match('Buddy','Buddy') and not bare_attention_match('Budy','Buddy')
    assert playback_contains_attention('现在小智准备继续。','小知')
    assert all(0<len(v)<=16 and all(re.fullmatch(r'[a-züêv]{1,8}',x) for x in v) for v in SYLLABLES.values())


@pytest.mark.parametrize('word',['停','123','小 知','x'*13])
def test_invalid_attention_word_never_decodes(word):
    media=Media('小知');result=recognize('小知',request(attention_wake_word=word),media)
    assert result['status']=='reference_unavailable' and media.calls==[]

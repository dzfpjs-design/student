import asyncio
import json

import pytest

from home_study_agent.pet_audio_gateway import decode_feedback, read_sse, PetAudioProtocolError


def feedback():
    return {'schema':'pet_audio_formative_v1','observations':[
        {'dimension':d,'status':'unknown','start_ms':None,'end_ms':None,
         'observation':'','exercise':'','uncertainty':'Synthetic fixture, not an actual voice judgment'}
        for d in ('audio_text_alignment','intelligibility','pronunciation','prosody','fluency')]}


def event(value):
    return ('data: '+json.dumps(value,ensure_ascii=False)+'\r\n\r\n').encode()


def stream(content, finish='stop', done=True):
    return event({'model':'synthetic-omni','choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]}) + event(
        {'choices':[{'index':0,'delta':{},'finish_reason':finish}]}) + (b'data: [DONE]\n\n' if done else b'')


def read(raw):
    async def chunks():
        for byte in raw:
            yield bytes([byte])
    return asyncio.run(read_sse(chunks()))


def test_split_utf8_and_terminal_stream_produces_formative_data_only():
    value=feedback()
    value['observations'][0]['uncertainty']='需要再听原音'
    result=read(stream(json.dumps(value,ensure_ascii=False)))
    assert decode_feedback(result['text'],duration_ms=1000)==value
    assert result['reported_model']=='synthetic-omni'


@pytest.mark.parametrize('raw', [b'data: [DONE]\n\n',stream('{}',done=False),
    stream('{}',finish='length'),b'data: {"choices":[],"choices":[]}\n\n',
    b'data: '+b'x'*140000,b'data: \xff\n\n'])
def test_incomplete_or_unbounded_stream_is_not_feedback(raw):
    with pytest.raises(PetAudioProtocolError): read(raw)


@pytest.mark.parametrize('fault',['score','action','outside_audio','nonfinite','duplicate','mastery_claim'])
def test_model_cannot_authorize_scoring_or_invent_audio_intervals(fault):
    value=feedback()
    if fault in {'score','action'}: value[fault]=1 if fault=='score' else {'tool':'run_shell'}
    elif fault=='outside_audio': value['observations'][0].update(status='observed',start_ms=0,end_ms=2000,observation='Clear phrase',exercise='Try once')
    elif fault=='nonfinite': value['observations'][0]['start_ms']=float('nan')
    elif fault=='duplicate': value['observations'][1]=dict(value['observations'][0])
    else: value['observations'][0].update(status='observed',start_ms=0,end_ms=1000,observation='你已经掌握PET。',exercise='Try once')
    with pytest.raises(PetAudioProtocolError): decode_feedback(json.dumps(value),duration_ms=1000)

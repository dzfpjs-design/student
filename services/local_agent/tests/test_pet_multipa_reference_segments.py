"""Synthetic bounded-frame and authority coverage; no weights/Windows claim."""
from types import SimpleNamespace
import copy
import numpy as np
import pytest
from home_study_agent.pet_multipa_reference_segments import (
    geometry,WindowedEmissions,plan,validate_result,ADAPTER,RESULT,SHORT_FRAMES)


class SyntheticEmissions:
    config=SimpleNamespace(conv_kernel=[10,3,3,3,3,2,2],conv_stride=[5,2,2,2,2,2,1])
    def __init__(self):
        self.calls=[]
        self.wav2vec2=SimpleNamespace(feature_extractor=SimpleNamespace(conv_layers=[SimpleNamespace(conv=SimpleNamespace(kernel_size=(k,),stride=(s,),padding=(0,),dilation=(1,)))for k,s in zip(self.config.conv_kernel,self.config.conv_stride)]))
    def __call__(self,values):
        self.calls.append((float(values[0,0]),values.shape[1]))
        step,field=geometry(self.config);n=(values.shape[1]-field)//step+1
        positions=values[:,np.arange(n)*step]
        return SimpleNamespace(logits=np.stack((positions,positions+1),axis=-1))


SYNTHETIC_TORCH=SimpleNamespace(isfinite=np.isfinite,cat=lambda parts,dim:np.concatenate(parts,axis=dim))


@pytest.mark.parametrize('frames',[240000,240001,560000,960000,9600000])
def test_original_grid_full_coverage_without_duplicate_or_missing_frames(frames):
    delegate=SyntheticEmissions();checks=[]
    wrapper=WindowedEmissions(delegate,SYNTHETIC_TORCH,lambda:checks.append(True))
    values=np.arange(frames,dtype=np.float64).reshape(1,-1)
    result=wrapper(values).logits;step,field=geometry(delegate.config)
    expected=np.arange((frames-field)//step+1)*step
    assert np.array_equal(result[0,:,0],expected)
    assert all(length<240000 for start,length in delegate.calls)
    assert len(checks)==2*len(delegate.calls)+1 and len(delegate.calls)>=2


@pytest.mark.parametrize('bad',[SimpleNamespace(),SimpleNamespace(conv_kernel=[10],conv_stride=[0]),SimpleNamespace(conv_kernel=[True],conv_stride=[2]),SimpleNamespace(conv_kernel=[2],conv_stride=[3]),SimpleNamespace(conv_kernel=[512]*10,conv_stride=[16]*10)])
def test_unknown_or_unsupported_geometry_is_not_guessed(bad):
    with pytest.raises(ValueError):geometry(bad)


@pytest.mark.parametrize('fault',['length','nan'])
def test_wrong_emission_grid_or_nonfinite_result_rejected(fault):
    delegate=SyntheticEmissions();original=delegate.__class__.__call__
    class Broken(SyntheticEmissions):
        def __call__(self,values):
            result=original(self,values)
            if fault=='length':result.logits=result.logits[:,:-1,:]
            else:result.logits=result.logits.astype(float);result.logits[0,0,0]=np.nan
            return result
    with pytest.raises(ValueError):WindowedEmissions(Broken(),SYNTHETIC_TORCH,lambda:None)(np.arange(480000).reshape(1,-1))


def test_cancellation_after_first_window_prevents_later_model_calls():
    delegate=SyntheticEmissions()
    def check():
        if delegate.calls:raise ValueError('PUBLIC_SYNTHETIC_CANCELLED')
    with pytest.raises(ValueError,match='PUBLIC_SYNTHETIC_CANCELLED'):
        WindowedEmissions(delegate,SYNTHETIC_TORCH,check)(np.arange(480000).reshape(1,-1))
    assert len(delegate.calls)==1


def fixture_plan():
    words=['we','read','one','page','and','read','another','page']
    aligned=[(i*4,i*4+1,w)for i,w in enumerate(words)]
    return words,aligned,plan(30*16000,words,aligned)


def test_authoritative_word_order_and_all_audio_frames_covered_once():
    words,aligned,p=fixture_plan()
    assert [w for s in p for w in s['text'].split()]==words
    assert p[0]['start_frame']==p[0]['start_word']==0
    assert p[-1]['end_frame']==30*16000 and p[-1]['end_word']==len(words)
    for a,b in zip(p,p[1:]):assert a['end_frame']==b['start_frame'] and a['end_word']==b['start_word']
    assert all(0<s['end_frame']-s['start_frame']<SHORT_FRAMES for s in p)


@pytest.mark.parametrize('fault',['missing','extra','different','reversed','overlap','long_word','leading_silence','nan'])
def test_no_asr_substitution_partial_word_coverage_or_unbounded_chunk(fault):
    words,aligned,_=fixture_plan()
    if fault=='missing':aligned.pop()
    elif fault=='extra':aligned.append((29,29.5,'extra'))
    elif fault=='different':aligned[2]=(8,9,'ASR_SUBSTITUTE')
    elif fault=='reversed':aligned=aligned[::-1]
    elif fault=='overlap':aligned[2]=(4,9,'one')
    elif fault=='long_word':aligned=[(0,16,'we')];words=['we']
    elif fault=='leading_silence':aligned=[(20,21,'we')];words=['we']
    else:aligned[0]=(0,float('nan'),'we')
    with pytest.raises(ValueError):plan(30*16000,words,aligned)


def numerical_result():
    words,aligned,p=fixture_plan();rows=[]
    for segment in p:
        a,b=segment['start_word'],segment['end_word'];count=b-a
        rows.append({k:segment[k]for k in('start_frame','end_frame','start_word','end_word')})
        rows[-1].update(sentence=dict(uttr_acc=.1,uttr_fluency=.2,uttr_prosodic=.3,uttr_total=.4),words=dict(word_acc=[.1]*count,word_stress=[.2]*count,word_total=[.3]*count),word_frames=[[round(a*16000),round(b*16000)]for a,b,w in aligned[a:b]])
    return dict(schema=RESULT,adapter=ADAPTER,audio_sha256='a'*64,reference_sha256='b'*64,reference_utf8_bytes=40,frames=480000,reference_word_count=len(words),alignment_frame_count=2998,segments=rows)


def test_raw_segment_results_do_not_create_an_utterance_average_or_text():
    value=numerical_result();assert validate_result(value)is value
    assert 'sentence'not in value and 'score'not in value and 'pass'not in value
    for bad in [dict(value,text='PRIVATE'),dict(value,sentence=value['segments'][0]['sentence']),dict(value,frames=True)]:
        with pytest.raises(ValueError):validate_result(bad)
    for field in('start_frame','start_word','end_word'):
        bad=copy.deepcopy(value);bad['segments'][1][field]+=1
        with pytest.raises(ValueError):validate_result(bad)


def test_actual_encoder_geometry_must_match_config_without_padding_or_dilation():
    for field,value in [('padding',(1,)),('dilation',(2,)),('kernel_size',(11,)),('stride',(1,))]:
        delegate=SyntheticEmissions();setattr(delegate.wav2vec2.feature_extractor.conv_layers[0].conv,field,value)
        with pytest.raises(ValueError,match='REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED'):
            WindowedEmissions(delegate,SYNTHETIC_TORCH,lambda:None)


def test_reviewed_long_budget_is_independent_bounded_and_not_a_caller_flag():
    from home_study_agent.pet_multipa_reference_segments import capable,budget
    declaration=dict(version=ADAPTER,deployment_sha256='a'*64,work_seconds=60,max_audio_frames=960000)
    approval=dict(authority_sha256={'deployment.json':'a'*64},reference_segment_adapter=declaration,timeout_seconds=1)
    assert capable(approval,frames=480000)and budget(approval,480000)==60
    for delta in [dict(work_seconds=True),dict(work_seconds=601),dict(work_seconds=15),dict(max_audio_frames=9600001),dict(deployment_sha256='b'*64),dict(caller=True)]:
        bad=dict(approval,reference_segment_adapter=dict(declaration,**delta))
        assert not capable(bad)
    for frames in [True,239999,960001]:
        assert not capable(approval,frames=frames)
        with pytest.raises(ValueError):budget(approval,frames)


def test_versioned_long_protocol_does_not_accept_short_or_arbitrary_output():
    from home_study_agent import pet_multipa_protocol as protocol
    value=numerical_result();nonce='c'*32
    packet=protocol.encode(nonce,result=value,reference=True,segmented=True)
    assert protocol.decode(packet,nonce,0,reference=True,segmented=True)==value
    for kwargs in [dict(),dict(reference=True),dict(segmented=True)]:
        with pytest.raises(ValueError):protocol.decode(packet,nonce,0,**kwargs)
    with pytest.raises(ValueError):protocol.encode(nonce,result=dict(value,transcript='PRIVATE'),reference=True,segmented=True)


def test_dense_matrix_cap_is_exact_and_cannot_be_raised_by_deployment_flags():
    from home_study_agent.pet_multipa_reference_segments import alignment_bound,MAX_ALIGNMENT_CELLS
    assert MAX_ALIGNMENT_CELLS==4000000
    assert alignment_bound(4000000,1,1,[0,1,0])==4000000
    for args in [(4000001,1,1,[0,1,0]),(4000000,1,1,[0,1,2,0]),(480000,160,400,[0,True,0]),(480000,160,400,[0,999,0])]:
        with pytest.raises(ValueError,match='REFERENCE_ALIGNMENT_MATRIX_BOUND'):alignment_bound(*args)

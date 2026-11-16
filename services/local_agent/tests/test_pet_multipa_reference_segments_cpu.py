"""Actual long CPU method + unchanged upstream API, synthetic learned dependencies."""
import ast,io,string,time,wave
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import numpy as np
import pytest
from test_pet_multipa_reference_segments import SyntheticEmissions
from home_study_agent.pet_multipa_reference import text_input
from home_study_agent.pet_multipa_cpu_engine import WorkerCPUModel
from home_study_agent import pet_multipa_cpu_engine


class Tensor:
    def __init__(self,value):self.value=np.asarray(value);self.shape=self.value.shape
    def __getitem__(self,index):return Tensor(self.value[index])
    def numpy(self):return self.value
    def cpu(self):return self
    def detach(self):return self
    def to(self,*a):return self
    def float(self):return self
    def unsqueeze(self,axis):return Tensor(np.expand_dims(self.value,axis))


def model_fixture(on_compute=lambda:None,dynamic_alignment=False):
    seen=dict(windows=[],features=[],ASR=0,forward=0,checks=0)
    source=Path(pet_multipa_cpu_engine.__file__).parent/'_multipa_cpu_vendor'
    api_node=next(n for n in ast.parse((source/'api.py').read_text()).body if isinstance(n,ast.FunctionDef)and n.name=='inference_one_seg')
    utility_tree=ast.parse((source/'utils_assessment.py').read_text())
    normalize=next(n for n in utility_tree.body if isinstance(n,ast.FunctionDef)and n.name=='remove_pun_except_apostrophe')
    class Delegate(SyntheticEmissions):
        def __call__(self,values):
            out=super().__call__(values.value);seen['windows'].append(values.shape[1]);return SimpleNamespace(logits=Tensor(out.logits))
    class Aligner:
        resolution=.01
        def __init__(self):
            self.aligner=Delegate();self.charsiu_processor=SimpleNamespace(_get_words=lambda text:text.split(),get_phones_and_words=lambda text:([('W',)]*len(text.split()),text.split()),get_phone_ids=lambda phones:[0]+[1 for _ in phones]+[0])
        def align(self,audio,text):
            # Exercise the real window adapter the copied coarse aligner received.
            output=self.aligner(Tensor(audio.reshape(1,-1)));assert output.logits.shape[1]==(len(audio)-400)//160+1
            words=text.split();interval=len(audio)/16000/len(words)if dynamic_alignment else 4
            return None,[(i*interval,i*interval+interval/4,w)for i,w in enumerate(words)],words,None,None,None
    def load(path):
        with wave.open(str(path),'rb')as w:return Tensor(np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32).reshape(1,-1)),w.getframerate()
    def asr(*a):seen['ASR']+=1;return 'wrong observed audio words'
    def features(audio,reference,observed,**kw):
        words=reference.split();seen['features'].append((reference,observed));n=len(words)
        return [(i*.1,i*.1+.08,w)for i,w in enumerate(words)],np.zeros((n,1)),np.zeros((n,1)),np.zeros((n,142)),np.zeros((n,768)),np.zeros((n,768)),[[i]for i in range(n)]
    def forward(*a):
        seen['forward']+=1;on_compute();n=a[1].shape[1]
        return *(Tensor([v])for v in(.1,.2,.3,.4)),*(Tensor([[v]*n])for v in(.5,.6,.7))
    torch=SimpleNamespace(inference_mode=nullcontext,reshape=lambda x,s:Tensor(np.reshape(x.value,s)),from_numpy=Tensor,isfinite=lambda x:np.isfinite(x.value),cat=lambda a,dim:Tensor(np.concatenate([x.value for x in a],axis=dim)),cuda=SimpleNamespace(empty_cache=lambda:None))
    env=dict(torch=torch,string=string,convert_num_to_word=lambda text:text,feature_extraction=features,aligment_model=object(),word_model=object(),device='SYNTHETIC',SAMPLE_RATE=16000,assessment_model=forward)
    exec(compile(ast.Module(body=[normalize,api_node],type_ignores=[]),str(source/'api.py'),'exec'),env)
    api=SimpleNamespace(torchaudio=SimpleNamespace(load=load),get_transcript=asr,inference_one_seg=env['inference_one_seg'],remove_pun_except_apostrophe=env['remove_pun_except_apostrophe'],convert_num_to_word=lambda text:text,aligment_model=Aligner())
    def check():seen['checks']+=1
    model=object.__new__(WorkerCPUModel);model.api=api;model.torch=torch;model.whisper_w=None;model.lease=SimpleNamespace(check=check,approved={'authority_sha256':{'deployment.json':'a'*64},'reference_segment_adapter':{'version':'multipa_reference_aligned_segments_v1','deployment_sha256':'a'*64,'work_seconds':60,'max_audio_frames':960000}})
    return model,seen


def long_wav(path):
    with wave.open(str(path),'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*480000)


def test_original_reference_full_cpu_method_routes_authoritative_segments_to_original_api(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture()
    value=model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()+10)
    assert value['frames']==480000 and value['reference_word_count']==8 and len(value['segments'])>=2
    assert seen['ASR']==seen['forward']==len(value['segments'])
    assert [w for reference,observed in seen['features']for w in reference.split()]==['we','read','one','page','and','read','another','page']
    assert all(reference!=observed for reference,observed in seen['features'])
    assert all(n<240000 for n in seen['windows'])and seen['checks']>len(seen['windows'])
    assert type(model.api.aligment_model.aligner).__name__=='Delegate'  # Original was not replaced.
    assert 'text'not in value and 'sentence'not in value and 'score'not in value


def test_segment_model_failure_stops_following_segments(tmp_path):
    path=tmp_path/'public.wav';long_wav(path)
    def stop():raise ValueError('PUBLIC_SEGMENT_CANCELLED')
    model,seen=model_fixture(stop)
    with pytest.raises(ValueError,match='PUBLIC_SEGMENT_CANCELLED'):
        model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()+10)
    assert seen['forward']==1


def test_expired_original_total_work_budget_prevents_any_model_call(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture()
    with pytest.raises(ValueError,match='WORKER_TIMEOUT'):
        model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()-1)
    assert seen['windows']==[]and seen['ASR']==seen['forward']==0


# Original vendored tokenizer/phone method source, with explicit dependency
# boundaries: no NLTK/G2P/num2words/weights are installed or downloaded here.
def install_original_tokenizer(model):
    import re,unicodedata,itertools
    source=Path(pet_multipa_cpu_engine.__file__).parent/'_multipa_cpu_vendor'
    cls=next(n for n in ast.parse((source/'processors.py').read_text()).body if isinstance(n,ast.ClassDef)and n.name=='CharsiuPreprocessor_en')
    methods=[next(n for n in cls.body if isinstance(n,ast.FunctionDef)and n.name==key)for key in('_get_words','get_phones_and_words')]
    def tokenize(text):
        # Controlled contraction boundary matching the named NLTK examples;
        # this is not a claim of installed NLTK/G2P acoustic integration.
        text=text.replace("can't","ca n't").replace("don't","do n't")
        return text.split()
    env=dict(re=re,unicodedata=unicodedata,unicode=str,normalize_numbers=lambda s:s,word_tokenize=tokenize,groupby=itertools.groupby)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='OriginalProcessor',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])),str(source/'processors.py'),'exec'),env)
    processor=env['OriginalProcessor']();processor.punctuation=set();processor.sil='[SIL]'
    processor.g2p=lambda text:list(itertools.chain.from_iterable([['W',' ']for _ in processor._get_words(text)]))[:-1]
    processor.get_phone_ids=lambda phones:[0]+[1 for _ in phones]+[0]
    model.api.aligment_model.charsiu_processor=processor
    # __copy__ keeps bound align callbacks on the original; assign an actual
    # class method so the existing shallow-copy/window adapter is exercised.
    type(model.api.aligment_model).align=lambda self,audio,text:(self.aligner(Tensor(audio.reshape(1,-1))),[(i*(len(audio)/16000/len(processor._get_words(text))),i*(len(audio)/16000/len(processor._get_words(text)))+.05,w)for i,w in enumerate(processor._get_words(text))],processor._get_words(text),None,None,None)
    utility=ast.parse((source/'utils_assessment.py').read_text());number=next(n for n in utility.body if isinstance(n,ast.FunctionDef)and n.name=='convert_num_to_word')
    number_env=dict(num2words=SimpleNamespace(num2words=lambda s:{'1':'one','2':'two','21':'twenty-one'}[s]))
    exec(compile(ast.Module(body=[number],type_ignores=[]),str(source/'utils_assessment.py'),'exec'),number_env)
    model.api.convert_num_to_word=number_env['convert_num_to_word']
    # Preserve original inference_one_seg but make the synthetic feature seam
    # use the same original processor word order rather than a whitespace split.
    env=model.api.inference_one_seg.__globals__
    def features(audio,reference,observed,**kw):
        words=processor.get_phones_and_words(reference)[1];count=len(words)
        model._token_seen.append(list(words))
        return [(i*.1,i*.1+.08,w)for i,w in enumerate(words)],np.zeros((count,1)),np.zeros((count,1)),np.zeros((count,142)),np.zeros((count,768)),np.zeros((count,768)),[[i]for i in range(count)]
    env['feature_extraction']=features;env['convert_num_to_word']=model.api.convert_num_to_word
    model._token_seen=[]
    return processor


@pytest.mark.parametrize('text',["we don't read one page and read another page", "we can't read one page and read another page", 'we read read read one page and another page', 'we read 1 page and read 2 pages'])
def test_original_tokenizer_is_authoritative_for_contractions_repetitions_and_numbers(tmp_path,text):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture(dynamic_alignment=True);processor=install_original_tokenizer(model)
    expected=processor._get_words(model.api.convert_num_to_word(model.api.remove_pun_except_apostrophe(text).lower()))
    value=model.predict_reference_segmented(path,text_input(text),deadline=time.monotonic()+10)
    assert value['reference_word_count']==len(expected)
    assert [w for piece in model._token_seen for w in piece]==expected
    assert all(len(piece)>0 for piece in model._token_seen)and seen['forward']==len(value['segments'])


def test_full_alignment_matrix_is_rejected_before_any_model_call(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture(dynamic_alignment=True)
    model.api.aligment_model.charsiu_processor.get_phone_ids=lambda phones:[0]+[1]*100000+[0]
    with pytest.raises(ValueError,match='REFERENCE_ALIGNMENT_MATRIX_BOUND'):
        model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()+10)
    assert seen['windows']==[]and seen['ASR']==seen['forward']==0


def test_retokenized_segment_must_preserve_complete_original_word_sequence(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture(dynamic_alignment=True)
    proc=model.api.aligment_model.charsiu_processor;original=proc.get_phones_and_words;calls=[]
    def changed(text):
        calls.append(text)
        phones,words=original(text)
        return phones,words if len(calls)==1 else words[:-1]
    proc.get_phones_and_words=changed
    with pytest.raises(ValueError,match='REFERENCE_FULL_WORD_COVERAGE_REQUIRED'):
        model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()+10)
    assert seen['windows']and seen['ASR']==seen['forward']==0


def test_number_hyphen_changed_by_original_api_roundtrip_is_not_silently_accepted(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture(dynamic_alignment=True);install_original_tokenizer(model)
    with pytest.raises(ValueError,match='REFERENCE_FULL_WORD_COVERAGE_REQUIRED'):
        model.predict_reference_segmented(path,text_input('we read 21 pages and read another page'),deadline=time.monotonic()+10)
    assert seen['windows']and seen['ASR']==seen['forward']==0


def test_observed_asr_dense_matrix_is_also_bounded_without_becoming_ground_truth(tmp_path):
    path=tmp_path/'public.wav';long_wav(path);model,seen=model_fixture(dynamic_alignment=True)
    proc=model.api.aligment_model.charsiu_processor;original=proc.get_phones_and_words
    proc.get_phones_and_words=lambda text:([('W',)]*100000,['wrong']*100000)if text.startswith('wrong')else original(text)
    with pytest.raises(ValueError,match='REFERENCE_ALIGNMENT_MATRIX_BOUND'):
        model.predict_reference_segmented(path,text_input('we read one page and read another page'),deadline=time.monotonic()+10)
    assert seen['ASR']==1 and seen['forward']==0

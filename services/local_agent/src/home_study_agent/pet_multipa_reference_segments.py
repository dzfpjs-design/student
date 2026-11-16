"""Independent bounded long-reference lane; raw segment data, never a score.

The original short-v1 adapter remains unchanged. All text is transient inside
one original owned worker. No acoustic model/installation is authorized here.
"""
import math
from types import SimpleNamespace
from .pet_multipa_epoch import need

ADAPTER = 'multipa_reference_aligned_segments_v1'
RESULT = 'pet_multipa_reference_segments_raw_v1'
MAX_FRAMES = 16000 * 600
MAX_BYTES = 24 * 1024 * 1024
SHORT_FRAMES = 16000 * 15
SENTENCE = {'uttr_acc','uttr_fluency','uttr_prosodic','uttr_total'}
WORDS = {'word_acc','word_stress','word_total'}


def capable(approved,*,frames=None):
    if type(approved) is not dict:return False
    authority=approved.get('authority_sha256');declaration=approved.get('reference_segment_adapter')
    if type(authority) is not dict or type(declaration) is not dict or set(declaration)!={'version','deployment_sha256','work_seconds','max_audio_frames'}:return False
    digest=authority.get('deployment.json')
    return (type(digest) is str and len(digest)==64 and all(c in '0123456789abcdef' for c in digest)
            and declaration['version']==ADAPTER and declaration['deployment_sha256']==digest
            and type(declaration['work_seconds'])is int and 15<=declaration['work_seconds']<=600
            and type(declaration['max_audio_frames'])is int and SHORT_FRAMES<=declaration['max_audio_frames']<=MAX_FRAMES
            and declaration['max_audio_frames']<=declaration['work_seconds']*16000
            and (frames is None or type(frames)is int and SHORT_FRAMES<=frames<=declaration['max_audio_frames']))


def budget(approved,frames):
    need(capable(approved,frames=frames),'REFERENCE_EXECUTOR_UNAVAILABLE')
    return approved['reference_segment_adapter']['work_seconds']


# Original forced_align calls dense librosa.dtw on frame x phone cost. Bound
# that matrix before either original alignment; Job memory/time remain final
# authority. 4M cells is a code cap, not a measured CPU/quality qualification.
MAX_ALIGNMENT_CELLS = 4000000


def alignment_bound(frames,step,field,phone_ids):
    need(type(frames)is int and 0<frames<=MAX_FRAMES and type(step)is int and step>0 and type(field)is int and field>0,'REFERENCE_ALIGNMENT_MATRIX_BOUND')
    need(type(phone_ids)is list and 3<=len(phone_ids)<=8194 and all(type(i)is int and 0<=i<512 for i in phone_ids),'REFERENCE_ALIGNMENT_MATRIX_BOUND')
    count=(frames-field)//step+1
    need(count>0 and count*(len(phone_ids)-2)<=MAX_ALIGNMENT_CELLS,'REFERENCE_ALIGNMENT_MATRIX_BOUND')
    return count


def geometry(config):
    kernels=getattr(config,'conv_kernel',None);strides=getattr(config,'conv_stride',None)
    need(type(kernels) in (list,tuple) and type(strides) in (list,tuple) and 1<=len(kernels)==len(strides)<=12,'REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED')
    step=field=1
    for kernel,stride in zip(kernels,strides):
        need(type(kernel)is int and type(stride)is int and 1<=kernel<=512 and 1<=stride<=16,'REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED')
        field+=(kernel-1)*step;step*=stride
        need(step<=1600 and field<=16000,'REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED')
    need(16000%step==0,'REFERENCE_ALIGNMENT_GRID_REQUIRED')
    return step,field


class WindowedEmissions:
    """Bounded window delegate for the original Charsiu align function only.

    Exact convolution geometry preserves frame locations. Overlap supplies
    context; only disjoint central logits survive. Windowed attention is a new
    adapter version and is not claimed equal to a full learned forward pass.
    """
    def __init__(self,delegate,torch,check):
        self.delegate=delegate;self.torch=torch;self.check=check
        self.step,self.field=geometry(delegate.config)
        extractor=getattr(getattr(delegate,'wav2vec2',None),'feature_extractor',None)
        layers=getattr(extractor,'conv_layers',None)
        need(layers is not None and len(layers)==len(delegate.config.conv_kernel),'REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED')
        for layer,kernel,stride in zip(layers,delegate.config.conv_kernel,delegate.config.conv_stride):
            conv=getattr(layer,'conv',None)
            need(conv is not None and tuple(conv.kernel_size)==(kernel,) and tuple(conv.stride)==(stride,)
                 and tuple(conv.padding)==(0,) and tuple(conv.dilation)==(1,),'REFERENCE_ALIGNMENT_GEOMETRY_REQUIRED')
        self.core=160000//self.step;self.context=16000//self.step
        need(self.core>0 and self.field+(self.core+2*self.context-1)*self.step<SHORT_FRAMES,'REFERENCE_ALIGNMENT_GRID_REQUIRED')

    def __call__(self,values):
        need(tuple(values.shape[:1])==(1,) and len(values.shape)==2,'REFERENCE_ALIGNMENT_INPUT_REQUIRED')
        total=int(values.shape[1]);need(type(total)is int and SHORT_FRAMES<=total<=MAX_FRAMES,'REFERENCE_LONG_PCM_REQUIRED')
        count=(total-self.field)//self.step+1;need(count>0,'REFERENCE_ALIGNMENT_GRID_REQUIRED')
        parts=[]
        for lo in range(0,count,self.core):
            self.check()
            hi=min(lo+self.core,count);window_lo=max(0,lo-self.context)
            start=window_lo*self.step;end=min(total,(hi+self.context-1)*self.step+self.field)
            need(0<=start<end<=total and end-start<SHORT_FRAMES,'REFERENCE_ALIGNMENT_WINDOW_BOUND')
            output=self.delegate(values[:,start:end]);self.check()
            logits=output.logits;expected=(end-start-self.field)//self.step+1
            need(len(logits.shape)==3 and tuple(logits.shape[:2])==(1,expected) and 2<=logits.shape[2]<=512,'REFERENCE_ALIGNMENT_LOGITS_GRID_CHANGED')
            need(bool(self.torch.isfinite(logits).all()),'REFERENCE_ALIGNMENT_LOGITS_REJECTED')
            offset=lo-window_lo;part=logits[:,offset:offset+hi-lo,:]
            need(tuple(part.shape[:2])==(1,hi-lo),'REFERENCE_ALIGNMENT_LOGITS_GRID_CHANGED')
            parts.append(part)
        self.check();merged=self.torch.cat(parts,dim=1)
        need(tuple(merged.shape[:2])==(1,count),'REFERENCE_ALIGNMENT_LOGITS_GRID_CHANGED')
        return SimpleNamespace(logits=merged)


def plan(frames,words,aligned):
    """Use only complete authoritative Charsiu word order, never ASR chunks."""
    need(type(frames)is int and SHORT_FRAMES<=frames<=MAX_FRAMES,'REFERENCE_LONG_PCM_REQUIRED')
    need(type(words)is list and 1<=len(words)<=2048 and all(type(w)is str and 0<len(w)<=256 for w in words),'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
    need(type(aligned)in(list,tuple) and len(aligned)<=8192,'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
    observed=[];spans=[];last=0
    for row in aligned:
        need(type(row)in(list,tuple) and len(row)==3 and type(row[2])is str,'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
        if row[2]=='[SIL]':continue
        need(all(type(v)in(int,float)and math.isfinite(v)for v in row[:2]),'REFERENCE_ALIGNMENT_FRAME_REQUIRED')
        start,end=round(row[0]*16000),round(row[1]*16000)
        need(last<=start<end<=frames,'REFERENCE_ALIGNMENT_FRAME_REQUIRED');last=end
        observed.append(row[2]);spans.append((start,end))
    need(observed==words,'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
    boundaries=[0]+[(spans[i-1][1]+spans[i][0])//2 for i in range(1,len(spans))]+[frames]
    result=[];first=0
    while first<len(words):
        ends=[j for j in range(first+1,len(words)+1)if 0<boundaries[j]-boundaries[first]<SHORT_FRAMES]
        need(bool(ends),'REFERENCE_WORD_BOUNDARY_SEGMENT_UNAVAILABLE')
        end=max(ends)
        result.append(dict(start_frame=boundaries[first],end_frame=boundaries[end],start_word=first,end_word=end,text=' '.join(words[first:end])))
        need(len(result)<=64,'REFERENCE_SEGMENT_COUNT_BOUND');first=end
    need(len(result)>=2 and result[0]['start_frame']==0 and result[-1]['end_frame']==frames,'REFERENCE_FULL_FRAME_COVERAGE_REQUIRED')
    return result


def validate_result(value):
    need(type(value)is dict and set(value)=={'schema','adapter','audio_sha256','reference_sha256','reference_utf8_bytes','frames','reference_word_count','alignment_frame_count','segments'},'REFERENCE_SEGMENTED_RESULT_REJECTED')
    need(value['schema']==RESULT and value['adapter']==ADAPTER,'REFERENCE_SEGMENTED_RESULT_REJECTED')
    for key in('audio_sha256','reference_sha256'):
        need(type(value[key])is str and len(value[key])==64 and all(c in'0123456789abcdef'for c in value[key]),'REFERENCE_SEGMENTED_RESULT_REJECTED')
    need(type(value['frames'])is int and SHORT_FRAMES<=value['frames']<=MAX_FRAMES and type(value['reference_utf8_bytes'])is int and 0<value['reference_utf8_bytes']<=24000,'REFERENCE_SEGMENTED_RESULT_REJECTED')
    need(type(value['reference_word_count'])is int and 1<=value['reference_word_count']<=2048 and type(value['alignment_frame_count'])is int and 0<value['alignment_frame_count']<=value['frames'],'REFERENCE_SEGMENTED_RESULT_REJECTED')
    segments=value['segments'];need(type(segments)is list and 2<=len(segments)<=64,'REFERENCE_SEGMENTED_RESULT_REJECTED')
    frame=word=0
    def number(v):return type(v)in(int,float)and math.isfinite(v)and abs(v)<=1000000
    for row in segments:
        need(type(row)is dict and set(row)=={'start_frame','end_frame','start_word','end_word','sentence','words','word_frames'},'REFERENCE_SEGMENTED_RESULT_REJECTED')
        need(all(type(row[k])is int for k in('start_frame','end_frame','start_word','end_word'))and row['start_frame']==frame and 0<row['end_frame']-frame<SHORT_FRAMES and row['end_frame']<=value['frames']and row['start_word']==word<row['end_word']<=value['reference_word_count'],'REFERENCE_SEGMENTED_RESULT_REJECTED')
        count=row['end_word']-word
        need(type(row['sentence'])is dict and set(row['sentence'])==SENTENCE and all(number(v)for v in row['sentence'].values()),'REFERENCE_SEGMENTED_RESULT_REJECTED')
        need(type(row['words'])is dict and set(row['words'])==WORDS and all(type(v)is list and len(v)==count and all(number(n)for n in v)for v in row['words'].values()),'REFERENCE_SEGMENTED_RESULT_REJECTED')
        spans=row['word_frames'];need(type(spans)is list and len(spans)==count,'REFERENCE_SEGMENTED_RESULT_REJECTED')
        for span in spans:
            need(type(span)is list and len(span)==2 and all(type(v)is int for v in span)and frame<=span[0]<span[1]<=row['end_frame'],'REFERENCE_SEGMENTED_RESULT_REJECTED');frame=span[1]
        frame=row['end_frame'];word=row['end_word']
    need(frame==value['frames']and word==value['reference_word_count'],'REFERENCE_SEGMENTED_RESULT_REJECTED')
    return value


def candidate(binding,result,asset_hashes,model):
    from .pet_multipa_epoch import GROUPS
    value=validate_result(result)
    need(binding['task_family']=='read_aloud'and value['frames']==binding['audio']['frames']and value['audio_sha256']==binding['audio']['sha256']and value['reference_sha256']==binding['reference']['text_sha256']and value['reference_utf8_bytes']==binding['reference']['text_utf8_bytes'],'REFERENCE_RESULT_BINDING_CHANGED')
    need(type(asset_hashes)is dict and set(asset_hashes)==GROUPS and all(type(v)is str and len(v)==64 and all(c in'0123456789abcdef'for c in v)for v in asset_hashes.values()),'REFERENCE_ASSETS_REQUIRED')
    return dict(schema=RESULT,adapter=ADAPTER,binding=binding,model=model,asset_hashes=dict(asset_hashes),raw_segments=value['segments'],word_count=value['reference_word_count'],alignment_frame_count=value['alignment_frame_count'],status='uncalibrated_model_candidate_not_rubric_score',assessor_installed=False,skill_qualified=False,mastery_claim_allowed=False,certification_claim_allowed=False,local_only=True,cloud_transmission_allowed=False)

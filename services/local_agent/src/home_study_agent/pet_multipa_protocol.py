"""Finite private worker protocol. No arbitrary errors, text logs or callbacks."""
import json,math
from .pet_multipa_epoch import need
MAX_RESULT=262144
RESULT_KEYS={'uttr_acc','uttr_fluency','uttr_prosodic','uttr_total','word_acc','word_stress','word_total','word_text','transcript_S','transcript_W'}
ERRORS={'UNCONFIGURED','RESOURCE_REJECTED','INPUT_REJECTED','MODEL_REJECTED','PROTOCOL_REJECTED','TIMEOUT','CANCELLED','MEMORY_LIMIT','PROCESS_REJECTED','OUTPUT_BOUND','CLEANUP_UNCONFIRMED'}
def validate_result(value):
    need(type(value)is dict and set(value)==RESULT_KEYS,'WORKER_RESULT_REJECTED')
    def number(x):return type(x)in(int,float)and math.isfinite(x)and abs(x)<=1000000
    need(all(number(value[k])for k in('uttr_acc','uttr_fluency','uttr_prosodic','uttr_total')),'WORKER_RESULT_REJECTED')
    words=value['word_text'];need(type(words)is list and 0<len(words)<=512 and all(type(x)is str and 0<len(x)<=80 for x in words),'WORKER_RESULT_REJECTED')
    for k in('word_acc','word_stress','word_total'):need(type(value[k])is list and len(value[k])==len(words)and all(number(x)for x in value[k]),'WORKER_RESULT_REJECTED')
    need(all(type(value[k])is str and 0<len(value[k])<=16000 for k in('transcript_S','transcript_W')),'WORKER_RESULT_REJECTED')
    need(len(json.dumps(value,allow_nan=False).encode())<=MAX_RESULT-4096,'WORKER_RESULT_REJECTED')
    return value

def encode(nonce,*,result=None,error=None,reference=False,segmented=False):
    need(type(nonce)is str and len(nonce)==32 and all(c in'0123456789abcdef'for c in nonce),'WORKER_PROTOCOL_REJECTED')
    need(type(reference)is bool and type(segmented)is bool and (not segmented or reference),'WORKER_PROTOCOL_REJECTED')
    schema = 'multipa_reference_segmented_worker_v1'if segmented else('multipa_reference_worker_v1' if reference else 'multipa_worker_v2')
    if error is None:
        if reference:
            if segmented:
                from .pet_multipa_reference_segments import validate_result as validate_reference
            else:
                from .pet_multipa_reference import validate_result as validate_reference
            result=validate_reference(result)
        else:result=validate_result(result)
        doc={'schema':schema,'nonce':nonce,'status':'completed','result':result}
    else:need(error in ERRORS,'WORKER_PROTOCOL_REJECTED');doc={'schema':schema,'nonce':nonce,'status':'failed','error':error}
    b=json.dumps(doc,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode();need(len(b)<=MAX_RESULT,'WORKER_PROTOCOL_REJECTED');return b

def decode(data,nonce,exitcode,*,reference=False,segmented=False):
    need(type(data)is bytes and 0<len(data)<=MAX_RESULT,'WORKER_PROTOCOL_REJECTED')
    try:doc=json.loads(data.decode('utf-8'))
    except(ValueError,UnicodeError):raise ValueError('PET_MULTIPA_WORKER_PROTOCOL_REJECTED')from None
    need(type(reference)is bool and type(segmented)is bool and (not segmented or reference),'WORKER_PROTOCOL_REJECTED')
    need(type(doc)is dict and doc.get('schema')==('multipa_reference_segmented_worker_v1'if segmented else('multipa_reference_worker_v1'if reference else'multipa_worker_v2'))and doc.get('nonce')==nonce,'WORKER_PROTOCOL_REJECTED')
    if doc.get('status')=='failed':
        need(set(doc)=={'schema','nonce','status','error'}and doc['error']in ERRORS and exitcode!=0,'WORKER_PROTOCOL_REJECTED')
        raise ValueError('PET_MULTIPA_WORKER_'+doc['error'])
    need(exitcode==0 and set(doc)=={'schema','nonce','status','result'}and doc.get('status')=='completed','WORKER_PROTOCOL_REJECTED')
    if reference:
        if segmented:
            from .pet_multipa_reference_segments import validate_result as validate_reference
        else:
            from .pet_multipa_reference import validate_result as validate_reference
        return validate_reference(doc['result'])
    return validate_result(doc['result'])

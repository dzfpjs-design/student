"""Fixed one-shot private worker. Never imported by the HTTP/model host.

The host owns both pipes and does not send the gate/input until the owned Job
has been assigned. A direct caller cannot authorize store writes through this
entry: no DB, source identifiers, callbacks or caller commands are accepted.
"""
import hashlib,json,os,struct,sys
from pathlib import Path

def main():
    # Preserve only a private protocol descriptor. Dependency prints, tracebacks,
    # stderr, file names and inferred transcripts never enter ordinary logs.
    protocol=os.dup(sys.stdout.fileno());os.set_inheritable(protocol,False)
    null=os.open(os.devnull,os.O_WRONLY)
    os.dup2(null,1);os.dup2(null,2);os.close(null)
    nonce='0'*32;packet=None;exitcode=20;reference_mode=False;segmented=False
    try:
        # Fixed bootstrap path, supplied by neither request nor environment.
        code=Path(__file__).absolute().parent.parent
        sys.path.insert(0,str(code))
        from home_study_agent.pet_multipa_epoch import WindowsEpochLease,need
        from home_study_agent.pet_multipa_protocol import encode
        prefix=sys.stdin.buffer.read(4);need(len(prefix)==4,'WORKER_PROTOCOL_REJECTED')
        n=struct.unpack('!I',prefix)[0];need(0<n<=1048576,'WORKER_PROTOCOL_REJECTED')
        header=json.loads(sys.stdin.buffer.read(n).decode('utf-8'))
        need(type(header)is dict,'WORKER_PROTOCOL_REJECTED')
        segmented=header.get('schema')=='multipa_reference_segmented_worker_input_v1'
        reference_mode=segmented or header.get('schema')=='multipa_reference_worker_input_v1'
        fields={'schema','nonce','key','approved','epoch','audio_bytes','audio_sha256'}
        need(set(header)==(fields|{'reference'}if reference_mode else fields),'WORKER_PROTOCOL_REJECTED')
        reference=None
        if reference_mode:
            from home_study_agent.pet_multipa_reference import validate_text,capable
            if segmented:
                from home_study_agent.pet_multipa_reference_segments import capable as long_capable
                need(long_capable(header['approved']),'REFERENCE_EXECUTOR_UNAVAILABLE')
            else:need(capable(header['approved']),'REFERENCE_EXECUTOR_UNAVAILABLE')
            reference=validate_text(header['reference'])
        nonce=header['nonce'];need(type(nonce)is str and len(nonce)==32 and all(c in'0123456789abcdef'for c in nonce),'WORKER_PROTOCOL_REJECTED')
        need(header['schema']==('multipa_reference_segmented_worker_input_v1'if segmented else('multipa_reference_worker_input_v1'if reference_mode else'multipa_worker_input_v2'))and type(header['key'])is str and type(header['audio_bytes'])is int and 0<header['audio_bytes']<=(24*1024*1024 if segmented else 4194304),'WORKER_PROTOCOL_REJECTED')
        content=sys.stdin.buffer.read(header['audio_bytes']);need(len(content)==header['audio_bytes']and sys.stdin.buffer.read(1)==b''and hashlib.sha256(content).hexdigest()==header['audio_sha256'],'WORKER_INPUT_REJECTED')
        with WindowsEpochLease(header['key'],parent_approved=header['approved'])as lease:
            need(lease.doc['epoch']==header['epoch']and Path(__file__).absolute()==lease.path('worker_script'),'WORKER_EPOCH_CHANGED')
            # Discard registry/site/environment search paths before any dependency
            # import; every path here belongs to the held, fully mapped epoch.
            sys.path[:]=[str(lease.path(k))for k in('code_directory','python_stdlib','python_dlls','python_packages')]
            os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_DATASETS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
            from home_study_agent.money_r4.money_files import OwnerDirectory
            from home_study_agent.registered_asset_snapshot import HeldFile,Policy
            work=Path(lease.approved['private_work_root'])
            # Parent created/holds this exact nonce child. All plaintext lives
            # beneath it, so parent cleanup covers forced worker termination too.
            directory=work/('multipa-'+nonce)
            with OwnerDirectory(work),OwnerDirectory(directory):
                path=directory/'original.wav'
                with path.open('xb')as stream:stream.write(content)
                bound=24*1024*1024 if segmented else 4194304
                policy=Policy(1,4,4096,1048576,bound,bound)
                with HeldFile(directory,'original.wav',policy)as held:
                    need(held.hash()[0]==header['audio_sha256'],'WORKER_INPUT_REJECTED')
                    # No downloader/subprocess or Python network fallback may be
                    # used by the reviewed offline dependency code.
                    def audit(event,args):
                        if event in {'socket.connect','socket.connect_ex','socket.getaddrinfo','subprocess.Popen','os.system'}:
                            raise PermissionError('PET_MULTIPA_OFFLINE_ONLY')
                    sys.addaudithook(audit)
                    from home_study_agent.pet_multipa_cpu_engine import WorkerCPUModel
                    if segmented:
                        import time
                        import wave
                        from home_study_agent.pet_multipa_reference_segments import budget
                        with wave.open(str(path),'rb')as wav:seconds=budget(lease.approved,wav.getnframes())
                        long_deadline=time.monotonic()+seconds
                    model=WorkerCPUModel(lease)
                    result=model.predict_reference_segmented(path,reference,deadline=long_deadline)if segmented else(model.predict_reference_original(path,reference)if reference_mode else model.predict_original(path))
                    held.check();packet=encode(nonce,result=result,reference=True,segmented=True)if segmented else(encode(nonce,result=result,reference=True)if reference_mode else encode(nonce,result=result));exitcode=0
    except BaseException:
        # Exception classes/descriptions are deliberately not serialized. Model
        # failures may contain original audio paths or inferred utterances.
        try:
            from home_study_agent.pet_multipa_protocol import encode
            packet=encode(nonce,error='MODEL_REJECTED',reference=True,segmented=True)if segmented else(encode(nonce,error='MODEL_REJECTED',reference=True)if reference_mode else encode(nonce,error='MODEL_REJECTED'))
        except BaseException:packet=b'{"schema":"multipa_worker_v2","nonce":"00000000000000000000000000000000","status":"failed","error":"PROTOCOL_REJECTED"}'
    finally:
        if packet is not None:
            with os.fdopen(protocol,'wb',closefd=True)as stream:stream.write(packet)
        else:os.close(protocol)
    return exitcode

if __name__=='__main__':raise SystemExit(main())

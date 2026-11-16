"""Host coordinator: no model imports or global dependency mutations here."""
from .pet_multipa_candidate import prepare_input,snapshot
from .pet_multipa_epoch import configured,need
from .pet_multipa_process import predict_in_worker
from .pet_speaking_candidate_store import SpeakingModelCandidateStore

class SpeakingOfflineCandidateAssessor:
    def __init__(self,database,*,media_root,deployment_key,data_encryption_key=None):
        self.database=database;self.media_root=media_root;self.key=deployment_key;self.data_encryption_key=data_encryption_key
        self.store=SpeakingModelCandidateStore(database,media_root=media_root,data_encryption_key=data_encryption_key)
    def assess(self,child_id,session_id,*,media_id,assessment_id,client_action_id,should_stop=lambda:False,job_claim=None):
        approved=configured(self.key)
        prior=self.store.recorded_action(child_id,session_id,media_id=media_id,assessment_id=assessment_id,asset_hashes=approved['asset_hashes'],client_action_id=client_action_id)
        if prior is not None:return prior # fresh lifecycle, no repeated inference
        reference=None;segmented=False
        with self.database.connect()as connection:
            with snapshot(connection):
                binding,content=prepare_input(connection,session_id,media_id=media_id,assessment_id=assessment_id,media_root=self.media_root,data_encryption_key=self.data_encryption_key)
                need(binding['child_id']==child_id,'CHILD_SCOPE_REQUIRED')
                if binding['task_family']=='read_aloud':
                    from .pet_multipa_reference import prepare_reference,capable
                    segmented=binding['audio']['frames']>=240000
                    if segmented:
                        from .pet_multipa_reference_segments import capable as long_capable
                        need(long_capable(approved,frames=binding['audio']['frames']),'REFERENCE_EXECUTOR_UNAVAILABLE')
                    else:need(capable(approved),'REFERENCE_EXECUTOR_UNAVAILABLE')
                    reference=prepare_reference(connection,binding)
                else:need(binding['task_family']=='spoken_response','REFERENCE_EXECUTOR_UNAVAILABLE')
        # Claimed durable work carries only the original host-private invocation.
        if job_claim is None:
            if segmented:result=predict_in_worker(self.key,content,should_stop=should_stop,reference=reference,segmented=True)
            else:result=predict_in_worker(self.key,content,should_stop=should_stop,reference=reference)if reference is not None else predict_in_worker(self.key,content,should_stop=should_stop)
            need(not should_stop(),'WORKER_CANCELLED')
            return self.store.record(child_id,session_id,binding=binding,result=result,asset_hashes=approved['asset_hashes'],client_action_id=client_action_id)
        from .pet_multipa_process import _begin_worker_completion,_predict_in_worker_attested,_discard_worker_completion
        invocation=_begin_worker_completion(job_claim,binding)
        try:
            attested=_predict_in_worker_attested(invocation,self.key,content,should_stop=should_stop,reference=reference,segmented=segmented)
            need(not should_stop(),'WORKER_CANCELLED')
            return self.store.record(child_id,session_id,binding=binding,result=attested.numeric,asset_hashes=approved['asset_hashes'],client_action_id=client_action_id,job_claim=job_claim,attested=attested,invocation=invocation)
        finally:
            _discard_worker_completion(invocation) # Also prior/commit error; HOLD keeps its original owner.
